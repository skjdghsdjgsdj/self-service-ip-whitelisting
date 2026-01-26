import datetime
import ipaddress
import os
from typing import Final

CACHE_TTL_SECONDS: Final[int] = os.environ.get("CACHE_TTL_SECONDS", 300)

import dotenv
from flask import Flask, request, abort, make_response
from peewee import MySQLDatabase, Model, CharField, IPField, DateTimeField

dotenv.load_dotenv()

db = MySQLDatabase(
    database = os.environ.get("MYSQL_DATABASE"),
    user = os.environ.get("MYSQL_USER"),
    password = os.environ.get("MYSQL_PASSWORD"),
    host = os.environ.get("MYSQL_HOST", "localhost"),
    port = os.environ.get("MYSQL_PORT", 3306)
)

class BaseModel(Model):
    class Meta:
        database = db

class TrustedIP(BaseModel):
    username = CharField(primary_key = True)
    ip = IPField()
    created = DateTimeField(default = datetime.datetime.now)
    modified = DateTimeField()

    class Meta:
        table_name = "trusted_ips"

app = Flask(__name__)

auth_cache: dict[str, tuple[bool, datetime.datetime]] = {} # IP to tuple of is trusted and expiry datetime
with app.app_context():
    for row in TrustedIP.select():
        auth_cache[row.ip] = (True, datetime.datetime.now() + datetime.timedelta(seconds = CACHE_TTL_SECONDS))

    app.logger.info(f"Populated auth cache with {len(auth_cache)} trusted IPs")

@app.route("/list", methods = ["GET"])
def list_trusted_ips():
    json = []
    for row in TrustedIP.select():
        _, expires = auth_cache.get(row.ip) or (None, None)

        json.append({
            "username": row.username,
            "ip": str(row.ip),
            "created": row.created.isoformat(),
            "modified": None if not row.modified else row.modified.isoformat(),
            "cache_ttl": None if expires is None else expires.isoformat(),
            "is_trusted": True
        })

    for ip, (is_trusted, expires) in auth_cache.items():
        for row in json:
            if ip == row["ip"]:
                continue

        json.append({
            "username": None,
            "ip": ip,
            "created": None,
            "modified": None,
            "cache_ttl": expires.isoformat(),
            "is_trusted": is_trusted
        })

    return json

def is_trusted(ip: str) -> bool:
    is_trusted, expires = auth_cache.get(ip) or (None, None)
    if is_trusted is None or expires <= datetime.datetime.now():
        expires = datetime.datetime.now() + datetime.timedelta(seconds = CACHE_TTL_SECONDS)
        is_trusted = TrustedIP.select().where(TrustedIP.ip == ip).exists()

        auth_cache[ip] = (is_trusted, expires)

        app.logger.info(f"Persisted to cache: IP {ip} is {'trusted' if is_trusted else 'not trusted'}, TTL {expires.isoformat()}")
    else:
        app.logger.debug(f"Cache hit: IP {ip} is {'trusted' if is_trusted else 'not trusted'}, TTL {expires.isoformat()}")

    return is_trusted

@app.route("/check", methods = ["GET"])
def check():
    try:
        client_ip = get_client_ip()
        if is_trusted(client_ip):
            return make_response("", 204)
        else:
            return make_response("Access denied", 403)
    except Exception as e:
        app.logger.error(e)
        abort(400)

@app.route("/trust_me", methods = ["GET"])
def trust_me():
    try:
        ip = get_client_ip()
        username = get_client_username()
    except Exception as e:
        app.logger.error(e)
        abort(400)

    with db.atomic() as transaction:
        try:
            existing_row = TrustedIP.select().where(TrustedIP.username == username).first()
            if not existing_row:
                app.logger.info(f"Trusting {username} at {ip} for the first time")
                TrustedIP(
                    username = username,
                    ip = ip
                ).save(force_insert = True)
            elif existing_row.ip != ip:
                app.logger.info(f"{username}'s IP changed from {existing_row.ip} to {ip}")
                existing_row.ip = ip
                existing_row.modified = datetime.datetime.now()
                existing_row.save()

                # cache bust the old IP
                auth_cache.pop(existing_row.ip)
            else:
                app.logger.info(f"{username}'s trusted IP is already {existing_row.ip}; nothing to do")

            auth_cache[ip] = (True, datetime.datetime.now() + datetime.timedelta(seconds = os.environ.get("CACHE_TTL_SECONDS", 300)))

        except Exception as e:
            app.logger.error(e)
            transaction.rollback()
            abort(500)

    return make_response("", 204)

def get_client_ip() -> str:
    client_ip_header = os.environ.get("CLIENT_IP_HEADER", "X-Forwarded-For")
    if client_ip_header not in request.headers:
        raise ValueError(f"No header \"{client_ip_header}\" in request")

    header_value = request.headers[client_ip_header]
    try:
        return str(ipaddress.ip_address(header_value))
    except ValueError as e:
        raise ValueError(f"IP \"{header_value}\" defined in header \"{client_ip_header}\" is not valid", e)

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


if __name__ == '__main__':
    app.run()
