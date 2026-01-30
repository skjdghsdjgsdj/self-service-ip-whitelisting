import ipaddress
import logging
import os
import redis
import dotenv
from flask import Flask, request, abort, make_response
from redis.backoff import ExponentialBackoff
from redis.retry import Retry

dotenv.load_dotenv()

app = Flask(__name__)
log_level_str = os.getenv('APP_LOG_LEVEL', 'INFO').upper()
app.logger.setLevel(getattr(logging, log_level_str, logging.INFO))

def init_redis_connection() -> redis.Redis:
    global redis_connection
    connection_args = dict(
        host = os.getenv("REDIS_HOST"),
        port = int(os.getenv("REDIS_PORT", 6379)),
        db = int(os.getenv("REDIS_DB", 0)),
        decode_responses = True,
        retry_on_timeout = True,
        retry = Retry(ExponentialBackoff(cap = 10, base = 1), 3),
        retry_on_error = [redis.exceptions.ConnectionError, redis.exceptions.TimeoutError],
        health_check_interval = 30,
        socket_timeout = 5,
        socket_connect_timeout = 5
    )
    password = os.getenv("REDIS_PASSWORD")
    if password:
        connection_args["password"] = password
        username = os.getenv("REDIS_USERNAME")
        if username:
            connection_args["username"] = username

    pool = redis.BlockingConnectionPool(max_connections = int(os.getenv("REDIS_MAX_CONNECTIONS", 10)), **connection_args)
    connection = redis.Redis(connection_pool = pool)

    return connection

with app.app_context():
    redis_connection = init_redis_connection()

def is_trusted(ip: str) -> bool:
    return redis_connection.exists(ip)

@app.route("/check", methods = ["GET"])
def check():
    try:
        client_ip = get_client_ip()
        if is_trusted(client_ip):
            return make_response("", 204)
        else:
            app.logger.info(f"Denying access to {client_ip}")
            return make_response("Access denied", 403)
    except Exception as e:
        app.logger.error(e)
        abort(400)

@app.route("/trust_me", methods = ["GET"])
def trust_me():
    try:
        new_ip = get_client_ip()
        if not new_ip:
            raise ValueError("No client IP header value provided in request")
        username = get_client_username()
        if not username:
            raise ValueError("No username header value provided in request")

        pipe = redis_connection.pipeline()

        # revoke trust for old IPs if they changed
        old_ips = list(redis_connection.smembers(f"user:{username}"))
        if old_ips:
            if len(old_ips) == 1 and old_ips[0] == new_ip: # only one trusted IP and it's the same thing
                app.logger.info(f"{username} only has one trusted IP and it's already {new_ip}")
                return make_response("", 204)

            app.logger.info(f"Revoking trust for old IP(s) for {username}: {', '.join(old_ips)}")
            pipe.delete(*old_ips)

        pipe.delete(f"user:{username}")

        pipe.hset(new_ip, mapping={"username": username})
        pipe.sadd(f"user:{username}", new_ip)
        pipe.execute()

        app.logger.info(f"Trusted IP {new_ip} for {username}")

        return make_response("", 204)
    except Exception as e:
        app.logger.error(f"Failed to manage trust state change: {e}")
        return make_response("", 400)


@app.route("/health", methods=["GET"])
def health():
    try:
        redis_connection.ping()
        return {
            "status": "ok"
        }
    except Exception as e:
        app.logger.error(f"Redis PING failure: {e}")
        return {
            "status": "error"
        }, 500

def get_client_ip() -> str:
    client_ip_header = os.getenv("CLIENT_IP_HEADER", "X-Forwarded-For")
    if client_ip_header not in request.headers:
        raise ValueError(f"No header '{client_ip_header}' in request")

    header_value = request.headers[client_ip_header].strip().split(',')[0].strip()

    try:
        return str(ipaddress.ip_address(header_value))
    except ValueError as e:
        raise ValueError(f"IP '{header_value}' in '{client_ip_header}' invalid", e)

def get_client_username() -> str:
    username_header = os.getenv("CLIENT_USERNAME_HEADER")
    if not username_header:
        raise RuntimeError("No environment variable CLIENT_USERNAME_HEADER defined")

    if username_header not in request.headers:
        raise ValueError(f"No header \"{username_header}\" in request")

    username = request.headers[username_header]
    if not username:
        raise ValueError(f"Header \"{username_header}\" is empty")

    return username

@app.errorhandler(500)
def handle_exception(e):
    app.logger.exception("Unhandled exception: %s", e)
    return "Internal Server Error", 500


if __name__ == '__main__':
    app.run()
