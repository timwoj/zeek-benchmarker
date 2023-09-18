import hmac
import os
import subprocess
import time
from datetime import datetime, timedelta

import redis
import rq
import zeek_benchmarker.tasks
from flask import Flask, current_app, jsonify, request
from werkzeug.exceptions import BadRequest, Forbidden


def is_allowed_build_url_prefix(url):
    """
    Is the given url a prefix in ALLOWED_BUILD_URLS
    """
    allowed_build_urls = current_app.config["ALLOWED_BUILD_URLS"]
    return any(url.startswith(allowed) for allowed in allowed_build_urls)


def verify_hmac(request_path, timestamp, request_digest, build_hash):
    # Generate a new version of the digest on this side using the same information that the
    # caller use to generate their digest, and then compare the two for validity.
    hmac_msg = f"{request_path:s}-{timestamp:d}-{build_hash:s}\n".encode()
    hmac_key = current_app.config["HMAC_KEY"].encode("utf-8")
    local_digest = hmac.new(hmac_key, hmac_msg, "sha256").hexdigest()
    if not hmac.compare_digest(local_digest, request_digest):
        current_app.logger.error(
            "HMAC digest from request ({:s}) didn't match local digest ({:s})".format(
                request_digest, local_digest
            )
        )
        return False

    return True


def check_hmac_request(req):
    """
    Remote requests are required to be signed with HMAC and have an sha256 hash
    of the build file passed with them.
    """
    hmac_header = req.headers.get("Zeek-HMAC", None)
    if not hmac_header:
        raise Forbidden("HMAC header missing from request")

    hmac_timestamp = int(req.headers.get("Zeek-HMAC-Timestamp", 0))
    if not hmac_timestamp:
        raise Forbidden("HMAC timestamp missing from request")

    # Double check that the timestamp is within the last 15 minutes UTC to avoid someone
    # trying to reuse it.
    ts = datetime.utcfromtimestamp(hmac_timestamp)
    utc = datetime.utcnow()
    delta = utc - ts

    if delta > timedelta(minutes=15):
        raise Forbidden("HMAC timestamp is outside of the valid range")

    build_hash = req.args.get("build_hash", "")
    if not build_hash:
        raise BadRequest("Build hash argument required")

    if not verify_hmac(req.path, hmac_timestamp, hmac_header, build_hash):
        raise Forbidden("HMAC validation failed")


def parse_request(req):
    """
    Generic request parsing.
    """
    req_vals = {}
    branch = request.args.get("branch", "")
    if not branch:
        raise BadRequest("Branch argument required")

    build_url = request.args.get("build", None)
    if not build_url:
        raise BadRequest("Build argument required")

    req_vals["build_hash"] = request.args.get("build_hash", "")

    # Validate that the build URL is allowed via the config, or local file from the local host.
    if is_allowed_build_url_prefix(build_url):
        check_hmac_request(request)
        remote_build = True
    elif build_url.startswith("file://") and request.remote_addr == "127.0.0.1":
        remote_build = False
    else:
        raise BadRequest("Invalid build URL")

    # Validate the branch name. Disallow semi-colon and then use git's
    # method for testing for valid names.
    if ";" in branch:
        raise BadRequest("Invalid branch name")

    ret = subprocess.call(
        ["git", "check-ref-format", "--branch", branch], stdout=subprocess.DEVNULL
    )
    if ret:
        raise BadRequest("Invalid branch name")

    # Normalize the branch name to remove any non-alphanumeric characters so it's
    # safe to use as part of a path name. This is way overkill, but it's safer.
    # Docker requires it to be all lowercase as well.
    hmac_timestamp = int(req.headers.get("Zeek-HMAC-Timestamp", 0))
    req_vals["original_branch"] = "".join(x for x in branch if x.isalnum()).lower()
    normalized_branch = req_vals["original_branch"]
    if remote_build:
        normalized_branch += f"-{int(hmac_timestamp):d}-{int(time.time()):d}"
    else:
        normalized_branch += f"-local-{int(time.time()):d}"

    req_vals["build_url"] = build_url
    req_vals["remote"] = remote_build
    req_vals["normalized_branch"] = normalized_branch
    req_vals["commit"] = request.args.get("commit", "")
    return req_vals


def enqueue_job(job_func, req_vals: dict[str, any]):
    """
    Enqueue the given request vals via redis rq for processing.
    """
    redis_host = os.getenv("REDIS_HOST", "localhost")
    queue_name = os.getenv("RQ_QUEUE_NAME", "default")
    queue_default_timeout = int(os.getenv("RQ_DEFAULT_TIMEOUT", "1800"))

    # New connection per request, that's alright for now.
    with redis.Redis(host=redis_host) as redis_conn:
        q = rq.Queue(
            name=queue_name,
            connection=redis_conn,
            default_timeout=queue_default_timeout,
        )

        return q.enqueue(job_func, req_vals)


def create_app(*, config=None):
    """
    Create the zeek-benchmarker app.
    """
    app = Flask(__name__)
    if config:
        app.config.update(config)

    @app.route("/zeek", methods=["POST"])
    def zeek():
        req_vals = parse_request(request)

        # At this point we've validated the request and just
        # enqueue it for the worker to pick up.
        job = enqueue_job(zeek_benchmarker.tasks.zeek_job, req_vals)
        return jsonify(
            {
                "job": {
                    "id": job.id,
                    "enqueued_at": job.enqueued_at,
                }
            }
        )

    @app.route("/broker", methods=["POST"])
    def broker():
        # At this point we've validated the request and just
        # enqueue it for the worker to pick up.
        req_vals = parse_request(request)
        job = enqueue_job(zeek_benchmarker.tasks.broker_job, req_vals)
        return jsonify(
            {
                "job": {
                    "id": job.id,
                    "enqueued_at": job.enqueued_at,
                }
            }
        )

    return app