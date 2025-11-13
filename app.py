import os
import json
import datetime
from typing import Optional, Tuple, Dict, Any

from urllib.parse import parse_qs

import requests
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from openai import OpenAI
import re

# =========================================
# Configurações básicas
# =========================================

app = FastAPI()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

KOMMO_DOMAIN = (os.getenv("KOMMO_DOMAIN") or "").rstrip("/")
KOMMO_TOKEN = os.getenv("KOMMO_TOKEN") or ""
AUTHORIZED_SUBDOMAIN = os.getenv("AUTHORIZED_SUBDOMAIN") or ""
ERIKA_ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID") or ""

ACTION_START = "### ERIKA_ACTION"
ACTION_END = "### END_ERIKA_ACTION"


def log(*args):
    print(datetime.datetime.now().isoformat(), "-", *args, flush=True)


# =========================================
# EXTRATOR DE TELEFONE UNIVERSAL — 360°
# =========================================

def extract_phone_intelligent(payload: dict) -> Optional[str]:
    """
    Extrator muito robusto que vasculha o payload inteiro do Kommo
    e encontra qualquer formato de telefone.
    """
    try:
        as_text = json.dumps(payload, ensure_ascii=False)
    except Exception:
        as_text = str(payload)

    # 1 — Buscar formato internacional padrão
    matches = re.findall(r"\+?\d{11,15}", as_text)
    if matches:
        # pega o maior número (geralmente o telefone real)
        return max(matches, key=len)

    # 2 — Detecta formato WABA
    waba = re.search(r"waba:\+?\d{11,15}", as_text)
    if waba:
        return waba.group().replace("waba:", "")

    # 3 — Busca campos explícitos no dict
    possible_keys = ["phone", "telefone", "mobile", "value", "tel"]

    def deep_search(obj):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if any(pk in k.lower() for pk in possible_keys):
                    if isinstance(v, str) and re.search(r"\+?\d{11,15}", v):
                        return v
                result = deep_search(v)
                if result:
                    return result
        elif isinstance(obj, list):
            for i in obj:
                result = deep_search(i)
                if result:
                    return result
        return None

    candidate = deep_search(payload)
    if candidate:
        candidate = re.sub(r"[^\d+]", "", candidate)
        if not candidate.startswith("+"):
            if candidate.startswith("55"):
                candidate = "+" + candidate
            else:
                candidate = "+55" + candidate
        return candidate

    return None


# =========================================
# Rotas básicas
# =========================================

@app.get("/")
async def root():
    return {"status": "ok", "message": "kommo-middleware online"}


@app.get("/health")
async def health():
    return {"status": "ok"}


# =========================================
# Split ERIKA_ACTION
# =========================================

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

    if not block:
        return visible, None

    try:
        parsed = json.loads(block)
    except Exception:
        log("Erro ao parsear ERIKA_ACTION:", block[:300])
        return visible, None

    return visible, parsed


# =========================================
# Helpers Kommo — notas e etapas
# =========================================

def add_kommo_note(lead_id: Optional[int], text: str):
    if not lead_id or not KOMMO_TOKEN or not KOMMO_DOMAIN:
        return

    url = f"{KOMMO_DOMAIN}/api/v4/leads/notes"
    payload = [{
        "entity_id": int(lead_id),
        "note_type": "common",
        "params": {"text": text}
    }]

    headers = {"Authorization": f"Bearer {KOMMO_TOKEN}"}

    log("-> Enviando nota ao Kommo:", text[:60], "...")
    try:
        requests.post(url, headers=headers, json=payload, timeout=20)
    except Exception as e:
        log("Erro ao enviar nota:", repr(e))


# =========================================
# Parser de Webhook form-urlencoded
# =========================================

def parse_kommo_form_urlencoded(body: bytes) -> Dict[str, Any]:
    text = body.decode("utf-8", "ignore")
    qs = parse_qs(text)

    def first(key):
        vals = qs.get(key)
        return vals[0] if vals else None

    def safe_int(v):
        try:
            return int(v)
        except Exception:
            return None

    message_text = (
        first("message[text]") or
        first("message[body]") or
        first("message[message]") or
        first("message[add][0][text]")
    )

    lead_id = safe_int(
        first("lead[id]") or
        first("leads[0][id]") or
        first("message[add][0][entity_id]")
    )

    phone = (
        first("contact[phones][0][value]") or
        first("contact[phone]") or
        first("phone")
    )

    payload = {
        "account": {"subdomain": first("account[subdomain]")},
        "data": {
            "message": {"text": message_text} if message_text else {},
            "lead": {"id": lead_id} if lead_id else {},
            "contact": {"phones": [{"value": phone}]} if phone else {}
        }
    }

    return payload


# =========================================
# ERIKA (OpenAI Assistant)
# =========================================

def call_openai_erika(user_message: str, lead_id=None, phone=None):
    if not ERIKA_ASSISTANT_ID:
        raise RuntimeError("OPENAI_ASSISTANT_ID não configurado")

    meta = []
    if lead_id:
        meta.append(f"lead_id={lead_id}")
    if phone:
        meta.append(f"telefone={phone}")

    msgs = [{"role": "user", "content": user_message}]
    if meta:
        msgs.append({"role": "user", "content": "[CONTEXTO KOMMO] " + " | ".join(meta)})

    log("-> Criando thread Erika")

    thread = client.beta.threads.create(messages=msgs)
    run = client.beta.threads.runs.create_and_poll(
        thread_id=thread.id,
        assistant_id=ERIKA_ASSISTANT_ID
    )

    messages = client.beta.threads.messages.list(thread_id=thread.id, limit=10)

    for m in messages.data:
        if m.role == "assistant":
            out = "\n".join(part.text.value for part in m.content if part.type == "text")
            log("-> Resposta Erika:", out[:120], "...")
            return out

    return ""


# =========================================
# HANDLER ESPECÍFICO — SALESBOT (WIDGET)
# =========================================

async def handle_salesbot_widget(payload: Dict[str, Any]) -> JSONResponse:
    """
    Fluxo quando o Kommo chama via Salesbot (handler=widget).
    Aqui quem devolve a mensagem pro WhatsApp é o próprio Kommo,
    usando o return_url.
    """
    log("===> MODO SALESBOT / WIDGET_REQUEST DETECTADO")

    token = payload.get("token")
    return_url = payload.get("return_url")
    data = payload.get("data") or {}

    # Dados que mandamos na etapa adaptada
    message_text = (
        data.get("message")
        or data.get("text")
        or ""
    )

    lead_id = (
        data.get("lead_id")
        or (data.get("lead") or {}).get("id")
    )

    contact = data.get("contact") or {}
    phone = (
        contact.get("phone")
        or contact.get("value")
        or extract_phone_intelligent(payload)
    )

    log("Salesbot → mensagem:", str(message_text)[:200])
    log("Salesbot → lead_id:", lead_id, "phone:", phone)

    if not str(message_text).strip():
        log("Salesbot sem mensagem → ignorado")
        return JSONResponse(
            {"status": "ignored", "reason": "widget sem mensagem"},
            status_code=200,
        )

    # Chama Erika
    try:
        ai_raw = call_openai_erika(message_text, lead_id=lead_id, phone=phone)
    except Exception as e:
        log("Erro ao chamar Erika (widget):", repr(e))
        if return_url and token:
            # Resposta de fallback para o cliente
            fallback = (
                "Tive um probleminha técnico agora 😅\n"
                "Pode me enviar a mensagem de novo em alguns instantes?"
            )
            try:
                requests.post(
                    return_url,
                    json={
                        "token": token,
                        "data": {"message": fallback},
                        "execute_handlers": [
                            {
                                "handler": "show",
                                "params": {
                                    "type": "text",
                                    "value": fallback,
                                },
                            }
                        ],
                    },
                    timeout=20,
                )
            except Exception as e2:
                log("Erro ao enviar fallback para return_url:", repr(e2))

        return JSONResponse(
            {"status": "error", "detail": "falha ao chamar Erika (widget)"},
            status_code=500,
        )

    visible, action = split_erika_output(ai_raw)
    reply = visible.strip() or "Oi! Sou a Erika, da TecBrilho. Como posso te ajudar hoje?"

    # Registra nota no lead
    if lead_id:
        try:
            add_kommo_note(lead_id, f"Erika 🧠 (Salesbot):\n{reply}")
            if action and isinstance(action, dict):
                summary = action.get("summary_note")
                if summary:
                    add_kommo_note(lead_id, f"ERIKA_ACTION: {summary}")
        except Exception as e:
            log("Erro ao registrar nota no modo widget:", repr(e))

    # Responder de volta para o Salesbot → isso aparece no WhatsApp
    if return_url and token:
        resp_payload = {
            "token": token,
            "data": {
                "message": reply
            },
            "execute_handlers": [
                {
                    "handler": "show",
                    "params": {
                        "type": "text",
                        "value": reply,
                    },
                }
            ],
        }
        try:
            log("Enviando resposta ao return_url do Salesbot:", return_url)
            r = requests.post(return_url, json=resp_payload, timeout=20)
            log("Status de retorno do Salesbot:", r.status_code, r.text[:200])
        except Exception as e:
            log("Erro ao postar no return_url do Salesbot:", repr(e))

    return JSONResponse(
        {
            "status": "ok",
            "mode": "salesbot_widget",
            "lead_id": lead_id,
            "phone": phone,
            "reply": reply,
            "erika_action": action,
        }
    )


# =========================================
# WEBHOOK PRINCIPAL
# =========================================

@app.post("/kommo-webhook")
async def kommo_webhook(request: Request):
    raw = await request.body()
    log("RAW (200b):", raw[:200])

    content_type = (request.headers.get("content-type") or "").lower()

    try:
        if "json" in content_type:
            payload = json.loads(raw.decode("utf-8"))
        else:
            payload = parse_kommo_form_urlencoded(raw)
    except Exception as e:
        log("Erro ao interpretar payload:", repr(e))
        raise HTTPException(400, "Payload inválido")

    log("Payload normalizado:", json.dumps(payload)[:800])

    # 🔹 1) Se for widget_request do Salesbot, tratamos em fluxo separado
    if payload.get("token") and payload.get("return_url") and payload.get("data"):
        return await handle_salesbot_widget(payload)

    # 🔹 2) Caso contrário, segue o fluxo antigo (webhook global)

    data = payload.get("data") or payload
    message_block = data.get("message") or {}

    message_text = (
        message_block.get("text") or
        message_block.get("body") or
        message_block.get("message") or
        data.get("text") or ""
    )

    if not message_text.strip():
        log("Sem mensagem → ignorado")
        return {"status": "ignored"}

    lead = data.get("lead") or {}
    lead_id = lead.get("id") or data.get("lead_id")

    # 🌟 EXTRAÇÃO UNIVERSAL DE TELEFONE
    phone = extract_phone_intelligent(payload)
    log("📞 Telefone extraído:", phone)

    ai_raw = call_openai_erika(message_text, lead_id=lead_id, phone=phone)
    visible, action = split_erika_output(ai_raw)

    reply = visible.strip() or "Oi! Sou a Erika da TecBrilho. Como posso ajudar?"

    if lead_id:
        add_kommo_note(lead_id, f"Erika 🧠:\n{reply}")

    return {
        "status": "ok",
        "lead_id": lead_id,
        "phone": phone,
        "reply": reply,
        "action": action,
    }
