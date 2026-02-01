import ipaddress
import logging
import os
from ipaddress import IPv4Network, IPv6Network

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
    trusted_subnets: list[IPv4Network | IPv6Network] = []
    for subnet_str in [subnet_str.strip() for subnet_str in os.getenv("TRUSTED_SUBNETS").split(",")]:
        try:
            subnet = ipaddress.ip_network(subnet_str)
        except ValueError as e:
            app.logger.warning(f"Ignoring subnet {subnet_str} as implicitly trusted because it can't be parsed: {e}")
            continue

        trusted_subnets.append(subnet)

    REDIS_PREFIX = os.getenv("REDIS_PREFIX", "ip-whitelist")

def is_trusted(ip: str) -> bool:
    return any(ipaddress.ip_address(ip) in subnet for subnet in trusted_subnets) or \
        redis_connection.exists(f"{REDIS_PREFIX}:{ip}")

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

        if any(ipaddress.ip_address(new_ip) in subnet for subnet in trusted_subnets):
            app.logger.info(f"Ignoring request to trust {new_ip} for {username} because it's already in a trusted subnet")
            return make_response("", 204)

        pipe = redis_connection.pipeline()

        # revoke trust for old IPs if they changed
        for old_ip_key in redis_connection.smembers(f"{REDIS_PREFIX}:user:{username}"):
            if not old_ip_key.startswith(f"{REDIS_PREFIX}:"):
                app.logger.warning(f"{username} is assocated to key {old_ip_key} which doesn't have prefix "
                                   f"{REDIS_PREFIX}:; it will be deleted.")
                pipe.delete(old_ip_key)
                continue

            old_ip = old_ip_key[len(f"{REDIS_PREFIX}:"):]
            if old_ip == new_ip:
                app.logger.info(f"{username} is already trusted at {new_ip}; ignoring request")
                return make_response("", 204)

            app.logger.info(f"Revoking trust for {username} at {old_ip}")
            pipe.delete(old_ip_key)

        # update the index to point to the new IP
        new_ip_key = f"{REDIS_PREFIX}:{new_ip}"
        pipe.delete(f"{REDIS_PREFIX}:user:{username}")
        pipe.hset(new_ip_key, mapping = {"username": username})
        pipe.sadd(f"{REDIS_PREFIX}:user:{username}", new_ip_key)
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
