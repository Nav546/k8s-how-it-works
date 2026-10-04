import os
import socket

import redis
from flask import Flask, jsonify

app = Flask(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "redis")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
REDIS_PASSWORD = os.getenv("REDIS_PASSWORD") or None
APP_TITLE = os.getenv("APP_TITLE", "Visit Counter")
APP_VERSION = os.getenv("APP_VERSION", "v1")


def get_client():
    return redis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD,
        socket_connect_timeout=2,
        socket_timeout=2,
        decode_responses=True,
    )


@app.route("/")
def index():
    info = {
        "title": APP_TITLE,
        "version": APP_VERSION,
        "served_by": socket.gethostname(),  # pod name when running in Kubernetes
    }
    try:
        info["visits"] = get_client().incr("visits")
        return jsonify(info), 200
    except redis.exceptions.RedisError as exc:
        info["error"] = f"cannot reach redis at {REDIS_HOST}:{REDIS_PORT} ({type(exc).__name__})"
        return jsonify(info), 503


@app.route("/healthz")
def healthz():
    # Deliberately does NOT check Redis: liveness = "is this process alive?"
    return jsonify(status="ok"), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
