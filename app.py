
import os
import json
import uuid
import hashlib
import secrets
from copy import deepcopy
from threading import RLock

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from storage import (
    init_db,
    save_task,
    get_task,
    list_tasks,
    get_message_record,
    save_message_record,
)
from agent import validate_proposal

app = FastAPI(title="A2A Invoice Agent", version="1.0.0")

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000/a2a/")
API_TOKEN = os.getenv("A2A_BEARER_TOKEN", "dev-token-change-me")

BATCH_MEDIA = "application/vnd.ga5.invoice-claim-batch+json"
PROPOSAL_MEDIA = "application/vnd.ga5.invoice-action-proposals+json"
RESULT_MEDIA = "application/vnd.ga5.invoice-action-results+json"
RECEIPT_MEDIA = "application/vnd.ga5.invoice-action-receipts+json"
A2A_MEDIA = "application/a2a+json"

receipt_lock = RLock()

init_db()


@app.get("/")
def home():
    return {"message": "A2A Invoice Agent is running"}


@app.get("/.well-known/agent-card.json")
def agent_card():
    return {
        "name": "A2A Invoice Agent",
        "description": (
            "Reviews invoice evidence and proposes safe business actions."
        ),
        "version": "1.0.0",
        "capabilities": {},
        "supportedInterfaces": [
            {
                "url": BASE_URL,
                "protocolBinding": "HTTP+JSON",
                "protocolVersion": "1.0",
            }
        ],
        "defaultInputModes": [BATCH_MEDIA],
        "defaultOutputModes": [PROPOSAL_MEDIA, RECEIPT_MEDIA],
        "skills": [
            {
                "id": "invoice_action_agent",
                "name": "Invoice Action Agent",
                "description": (
                    "Evaluates invoice evidence and proposes safe actions."
                ),
                "tags": ["invoice", "approval", "payment"],
            }
        ],
    }


@app.middleware("http")
async def check_a2a_headers(request: Request, call_next):
    path = request.url.path

    if path in ("/", "/.well-known/agent-card.json"):
        return await call_next(request)

    if path.startswith("/a2a/"):
        if request.headers.get("A2A-Version") != "1.0":
            return JSONResponse(
                status_code=400,
                content={"error": "INVALID_A2A_VERSION"},
            )

        if request.method == "POST":
            content_type = (
                request.headers.get("content-type", "")
                .split(";")[0]
                .strip()
            )
            if content_type != A2A_MEDIA:
                return JSONResponse(
                    status_code=415,
                    content={"error": "UNSUPPORTED_MEDIA_TYPE"},
                )

        authorization = request.headers.get("Authorization", "")
        expected = f"Bearer {API_TOKEN}"

        if not secrets.compare_digest(authorization, expected):
            return JSONResponse(
                status_code=401,
                content={"error": "UNAUTHORIZED"},
            )

    return await call_next(request)


def canonical_hash(message):
    encoded = json.dumps(
        message,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def get_message_parts(message):
    parts = message.get("parts", [])
    return parts if isinstance(parts, list) else []


def get_artifact_part(task, media_type):
    for artifact in task.get("artifacts", []):
        for part in artifact.get("parts", []):
            if part.get("mediaType") == media_type:
                return part
    return None


def make_proposals(batch):
    packages = batch.get("packages", [])

    if not isinstance(packages, list) or not packages:
        raise HTTPException(status_code=400, detail="Invalid packages")

    proposals = []
    seen_package_ids = set()

    for package in packages:
        if not isinstance(package, dict):
            raise HTTPException(status_code=400, detail="Invalid package")

        package_id = package.get("packageId")

        if not isinstance(package_id, str) or not package_id:
            raise HTTPException(status_code=400, detail="Missing packageId")

        if package_id in seen_package_ids:
            raise HTTPException(status_code=400, detail="Duplicate packageId")

        seen_package_ids.add(package_id)

        # Safe placeholder only. Real document evidence and policy
        # reasoning must replace this before grading.
        facts = package.get("facts", {})
        if not isinstance(facts, dict):
            facts = {}

        try:
            amount = int(facts.get("amountMinor", 0))
        except (TypeError, ValueError):
            amount = 0

        proposal = {
            "packageId": package_id,
            "actionId": uuid.uuid4().hex,
            "action": "open_exception",
            "facts": {
                "vendorName": str(facts.get("vendorName", "unknown")),
                "invoiceNumber": str(facts.get("invoiceNumber", "unknown")),
                "amountMinor": amount,
                "currency": str(facts.get("currency", "INR")),
            },
            "evidenceRefs": [],
            "rationale": (
                "open_exception: the invoice evidence has not yet been "
                "validated; manual review is required."
            ),
        }

        proposals.append(proposal)

    return proposals


def create_task(message, batch):
    batch_id = batch.get("batchId")

    if not isinstance(batch_id, str) or not batch_id:
        raise HTTPException(status_code=400, detail="Invalid batchId")

    if not isinstance(batch.get("policyRevision"), str):
        raise HTTPException(status_code=400, detail="Missing policyRevision")

    proposals = make_proposals(batch)

    task_id = str(uuid.uuid4())
    context_id = message.get("contextId") or str(uuid.uuid4())

    task = {
        "id": task_id,
        "contextId": context_id,
        "status": {"state": "TASK_STATE_INPUT_REQUIRED"},
        "history": [message],
        "artifacts": [
            {
                "artifactId": str(uuid.uuid4()),
                "parts": [
                    {
                        "mediaType": PROPOSAL_MEDIA,
                        "data": {
                            "batchId": batch_id,
                            "proposals": proposals,
                        },
                    }
                ],
            }
        ],
    }

    return task


def process_results(message, principal):
    task_id = message.get("taskId")
    context_id = message.get("contextId")

    if not isinstance(task_id, str) or not task_id:
        raise HTTPException(status_code=400, detail="Missing taskId")

    if not isinstance(context_id, str) or not context_id:
        raise HTTPException(status_code=400, detail="Missing contextId")

    task = get_task(task_id, principal)

    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.get("contextId") != context_id:
        raise HTTPException(status_code=409, detail="Context mismatch")

    if task.get("status", {}).get("state") != "TASK_STATE_INPUT_REQUIRED":
        raise HTTPException(status_code=409, detail="Task is not awaiting results")

    parts = get_message_parts(message)
    result_parts = [
        part for part in parts
        if part.get("mediaType") == RESULT_MEDIA
    ]

    if len(result_parts) != 1:
        raise HTTPException(status_code=400, detail="Expected one results part")

    result_data = result_parts[0].get("data")

    if not isinstance(result_data, dict):
        raise HTTPException(status_code=400, detail="Invalid results data")

    proposal_part = get_artifact_part(task, PROPOSAL_MEDIA)

    if proposal_part is None:
        raise HTTPException(status_code=409, detail="Stored proposals missing")

    proposal_data = proposal_part.get("data", {})
    batch_id = proposal_data.get("batchId")

    if result_data.get("batchId") != batch_id:
        raise HTTPException(status_code=409, detail="Batch mismatch")

    proposals_list = proposal_data.get("proposals", [])
    proposals = {p["packageId"]: p for p in proposals_list}

    results = result_data.get("results")

    if not isinstance(results, list) or len(results) != len(proposals):
        raise HTTPException(status_code=409, detail="Incomplete results")

    seen = set()
    executions = []

    for result in results:
        if not isinstance(result, dict):
            raise HTTPException(status_code=400, detail="Invalid result")

        package_id = result.get("packageId")
        proposal = proposals.get(package_id)

        if proposal is None or package_id in seen:
            raise HTTPException(status_code=409, detail="Unknown or repeated package")

        seen.add(package_id)

        if (
            result.get("actionId") != proposal["actionId"]
            or result.get("action") != proposal["action"]
            or result.get("outcome") not in ("ACCEPTED", "REJECTED")
        ):
            raise HTTPException(status_code=409, detail="Result mismatch")

        nonce = result.get("receiptNonce")
        if not isinstance(nonce, str) or not nonce:
            raise HTTPException(status_code=409, detail="Missing receipt nonce")

        if not validate_proposal(proposal):
            raise HTTPException(status_code=409, detail="Invalid proposal")

        if result["outcome"] == "ACCEPTED":
            executions.append(
                {
                    "packageId": package_id,
                    "actionId": proposal["actionId"],
                    "action": proposal["action"],
                    "receiptNonce": nonce,
                    "facts": deepcopy(proposal["facts"]),
                    "evidenceRefs": list(proposal["evidenceRefs"]),
                }
            )

    task["history"].append(message)

    if executions:
        task["artifacts"].append(
            {
                "artifactId": str(uuid.uuid4()),
                "parts": [
                    {
                        "mediaType": RECEIPT_MEDIA,
                        "data": {
                            "batchId": batch_id,
                            "executions": executions,
                        },
                    }
                ],
            }
        )

    task["status"] = {"state": "TASK_STATE_COMPLETED"}

    save_task(
        task_id,
        principal,
        context_id,
        "TASK_STATE_COMPLETED",
        task,
    )

    return task


@app.post("/a2a/message:send")
async def send_message(request: Request):
    body = await request.json()

    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="Invalid request")

    message = body.get("message")

    if not isinstance(message, dict):
        raise HTTPException(status_code=400, detail="Missing message")

    if message.get("role") != "ROLE_USER":
        raise HTTPException(status_code=400, detail="Invalid message role")

    message_id = message.get("messageId")

    if not isinstance(message_id, str) or not message_id:
        raise HTTPException(status_code=400, detail="Missing messageId")

    principal = request.headers["Authorization"][7:]
    message_hash = canonical_hash(message)
    parts = get_message_parts(message)

    is_result_message = any(
        part.get("mediaType") == RESULT_MEDIA for part in parts
    )

    with receipt_lock:
        existing = get_message_record(principal, message_id)

        if existing:
            if existing["message_hash"] != message_hash:
                raise HTTPException(
                    status_code=409,
                    detail="IDEMPOTENCY_CONFLICT",
                )
            return {"task": existing["task"]}

        if is_result_message:
            task = process_results(message, principal)
        else:
            batch_parts = [
                part for part in parts
                if part.get("mediaType") == BATCH_MEDIA
            ]

            if len(batch_parts) != 1:
                raise HTTPException(
                    status_code=400,
                    detail="Expected one invoice batch",
                )

            batch = batch_parts[0].get("data")

            if not isinstance(batch, dict):
                raise HTTPException(status_code=400, detail="Invalid batch data")

            task = create_task(message, batch)

        task_id = task["id"]
        save_task(
            task_id,
            principal,
            task["contextId"],
            task["status"]["state"],
            task,
        )

        save_message_record(
            principal,
            message_id,
            message_hash,
            task_id,
            task,
        )

        return {"task": task}


@app.get("/a2a/tasks")
def tasks(authorization: str = Header(...)):
    principal = authorization[7:]
    return {"tasks": list_tasks(principal)}


@app.get("/a2a/tasks/{task_id}")
def task_detail(task_id: str, authorization: str = Header(...)):
    principal = authorization[7:]
    task = get_task(task_id, principal)

    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")

    return task


@app.post("/a2a/tasks/{task_id}:cancel")
def cancel_task(task_id: str, authorization: str = Header(...)):
    principal = authorization[7:]

    with receipt_lock:
        task = get_task(task_id, principal)

        if task is None:
            raise HTTPException(status_code=404, detail="Task not found")

        state = task.get("status", {}).get("state")

        if state in ("TASK_STATE_COMPLETED", "TASK_STATE_CANCELED"):
            raise HTTPException(status_code=409, detail="Task is terminal")

        task["status"] = {"state": "TASK_STATE_CANCELED"}

        save_task(
            task_id,
            principal,
            task["contextId"],
            "TASK_STATE_CANCELED",
            task,
        )

        return task
