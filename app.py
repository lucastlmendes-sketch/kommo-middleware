import os
import json
import datetime
import re
from typing import Optional, Dict, Any, Tuple

import requests
from fastapi import FastAPI, Request, HTTPException
from openai import OpenAI

# =============================
# CONFIGURAÇÃO BÁSICA
# =============================

app = FastAPI()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
KOMMO_DOMAIN = os.getenv("KOMMO_DOMAIN", "").rstrip("/")
KOMMO_TOKEN = os.getenv("KOMMO_TOKEN", "")
ERIKA_ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID", "")

ACTION_START = "### ERIKA_ACTION"
ACTION_END = "### END_ERIKA_ACTION"


def log(*args):
    print(datetime.datetime.now().isoformat(), "-", *args, flush=True)


# ==========================================================
# EXTRATOR DE TELEFONE — UNIVERSAL
# ==========================================================

def extract_phone_intelligent(payload: dict) -> Optional[str]:
    try:
        text = json.dumps(payload, ensure_ascii=False)
    except:
        text = str(payload)

    match = re.findall(r"\+?\d{11,15}", text)
    if match:
        return max(match, key=len)

    return None


# ==========================================================
# API DO KOMMO — ENVIAR MENSAGEM WHATSAPP
# ==========================================================

def send_whatsapp_message(lead_id: int, phone: str, text: str):
    """
    Envia a mensagem diretamente para o WhatsApp via API Kommo.
    """

    url = f"{KOMMO_DOMAIN}/api/v4/messages"

    payload = [{
        "recipient": {
            "type": "lead",
            "id": lead_id
        },
        "message": {
            "text": text,
            "type": "text"
        },
        "origin": {
            "type": "whatsapp"
        }
    }]

    headers = {
        "Authorization": f"Bearer {KOMMO_TOKEN}",
        "Content-Type": "application/json"
    }

    log("📤 Enviando mensagem para WhatsApp:", payload)

    r = requests.post(url, headers=headers, json=payload, timeout=30)

    log("📨 Resposta envio para Kommo:", r.status_code, r.text)


# ==========================================================
# OPENAI — ERIKA
# ==========================================================

def split_erika_output(full: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    if not full:
        return "", None

    start = full.rfind(ACTION_START)
    if start == -1:
        return full.strip(), None

    visible = full[:start].rstrip()
    after = full[start + len(ACTION_START):]

    end = after.rfind(ACTION_END)
    block = after[:end] if end != -1 else after
    block = block.strip()

    try:
        parsed = json.loads(block)
    except:
        parsed = None

    return visible, parsed


def call_erika(user_message: str, lead_id=None, phone=None) -> str:
    msgs = [{"role": "user", "content": user_message}]

    meta = []
    if lead_id:
        meta.append(f"lead_id={lead_id}")
    if phone:
        meta.append(f"telefone={phone}")
    if meta:
        msgs.append({"role": "user", "content": "[CONTEXTO] " + " | ".join(meta)})

    thread = client.beta.threads.create(messages=msgs)
    run = client.beta.threads.runs.create_and_poll(
        thread_id=thread.id,
        assistant_id=ERIKA_ASSISTANT_ID
    )

    messages = client.beta.threads.messages.list(thread_id=thread.id, limit=10)

    for m in messages.data:
        if m.role == "assistant":
            return "\n".join(
                part.text.value for part in m.content if part.type == "text"
            )

    return "Olá! Como posso ajudar?"


# ==========================================================
# ROTAS
# ==========================================================

@app.get("/")
async def root():
    return {"status": "ok"}


@app.post("/kommo-webhook")
async def webhook(request: Request):
    raw = await request.body()
    log("RAW:", raw[:200])

    content_type = (request.headers.get("content-type") or "")

    try:
        if "json" in content_type:
            payload = json.loads(raw.decode("utf-8"))
        else:
            raise Exception("Formato inválido: precisa ser JSON")
    except Exception as e:
        log("Erro parse payload:", str(e))
        raise HTTPException(400, "Payload inválido")

    log("Payload:", json.dumps(payload)[:800])

    data = payload.get("data") or payload
    msg = data.get("message") or {}

    text = msg.get("text") or msg.get("body") or ""
    if not text.strip():
        log("Sem mensagem → ignore")
        return {"status": "ignored"}

    lead = data.get("lead") or {}
    lead_id = lead.get("id")
    if not lead_id:
        log("Sem lead_id → não posso responder")
        return {"status": "no-lead"}

    phone = extract_phone_intelligent(payload)
    log("📞 Telefone:", phone)

    log("🤖 Chamando Erika…")
    raw_reply = call_erika(text, lead_id=lead_id, phone=phone)

    reply, action = split_erika_output(raw_reply)
    reply = reply.strip() or "Olá! Como posso ajudar?"

    log("💬 Resposta da Erika:", reply)

    send_whatsapp_message(lead_id, phone, reply)

    return {"status": "ok", "sent": reply}
