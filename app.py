import os
import json
import datetime
from typing import Optional, Tuple, Dict, Any

import requests
from fastapi import FastAPI, Request, HTTPException
from openai import OpenAI
from urllib.parse import parse_qs

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
    """Log simples com timestamp (aparece nos logs do Render)."""
    print(datetime.datetime.now().isoformat(), "-", *args, flush=True)


# =========================================
# Rotas básicas (Render / Healthcheck)
# =========================================

@app.get("/")
async def root():
    return {"status": "ok", "message": "kommo-middleware online"}


@app.get("/health")
async def health():
    return {"status": "ok"}


# =========================================
# Helpers para ERIKA_ACTION
# =========================================

def split_erika_output(full_text: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """
    Separa o texto visível ao cliente do bloco técnico ERIKA_ACTION.
    Retorna (texto_visivel, action_dict_ou_None).
    """
    if not full_text:
        return "", None

    start = full_text.rfind(ACTION_START)
    if start == -1:
        # Nenhum bloco encontrado
        return full_text.strip(), None

    visible_text = full_text[:start].rstrip()

    after = full_text[start + len(ACTION_START):]
    end = after.rfind(ACTION_END)
    if end != -1:
        action_raw = after[:end]
    else:
        action_raw = after

    action_raw = action_raw.strip()

    if not action_raw:
        return visible_text, None

    try:
        action_data = json.loads(action_raw)
    except json.JSONDecodeError as e:
        log("Erro ao decodificar ERIKA_ACTION:", repr(e), "conteúdo:", action_raw[:500])
        action_data = None

    return visible_text, action_data


# =========================================
# Helpers para Kommo
# =========================================

def add_kommo_note(lead_id: Optional[int], text: str):
    """Cria uma nota 'common' no lead do Kommo."""
    if not lead_id or not KOMMO_DOMAIN or not KOMMO_TOKEN or not text:
        return

    url = f"{KOMMO_DOMAIN}/api/v4/leads/notes"
    payload = [
        {
            "entity_id": int(lead_id),
            "note_type": "common",
            "params": {
                "text": text
            }
        }
    ]
    headers = {"Authorization": f"Bearer {KOMMO_TOKEN}"}

    log("Enviando nota para Kommo:", url, "lead_id=", lead_id)
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()


# Mapeamento de nome da etapa -> variável de ambiente com o status_id do Kommo
STAGE_ENV_MAP = {
    "Leads Recebidos": "KOMMO_STATUS_LEADS_RECEBIDOS",
    "Contato em Andamento": "KOMMO_STATUS_CONTATO_EM_ANDAMENTO",
    "Serviço Vendido": "KOMMO_STATUS_SERVICO_VENDIDO",
    "Agendamento Pendente": "KOMMO_STATUS_AGENDAMENTO_PENDENTE",
    "Agendamentos Confirmados": "KOMMO_STATUS_AGENDAMENTOS_CONFIRMADOS",
    "Cliente Presente": "KOMMO_STATUS_CLIENTE_PRESENTE",
    "Cliente Ausente": "KOMMO_STATUS_CLIENTE_AUSENTE",
    "Reengajar": "KOMMO_STATUS_REENGAJAR",
    "Solicitar FeedBack": "KOMMO_STATUS_SOLICITAR_FEEDBACK",
    "Solicitar Avaliação Google": "KOMMO_STATUS_SOLICITAR_AVALIACAO_GOOGLE",
    "Avaliação 5 Estrelas": "KOMMO_STATUS_AVALIACAO_5_ESTRELAS",
    "Cliente Insatisfeito": "KOMMO_STATUS_CLIENTE_INSATISFEITO",
    "Vagas de Emprego": "KOMMO_STATUS_VAGAS_DE_EMPREGO",
    "Solicitar Atendimento Humano": "KOMMO_STATUS_SOLICITAR_ATENDIMENTO_HUMANO",
}


def update_lead_stage(lead_id: Optional[int], stage_name: Optional[str]):
    """Atualiza a etapa/status do lead no Kommo, se IDs estiverem configurados."""
    if not lead_id or not stage_name:
        return

    env_name = STAGE_ENV_MAP.get(stage_name)
    if not env_name:
        log("Nenhum env configurado para etapa:", stage_name)
        return

    status_id = os.getenv(env_name)
    if not status_id:
        log("Variável de ambiente não definida para", stage_name, "=>", env_name)
        return

    if not KOMMO_DOMAIN or not KOMMO_TOKEN:
        log("KOMMO_DOMAIN ou KOMMO_TOKEN não configurados, não foi possível mover o lead.")
        return

    url = f"{KOMMO_DOMAIN}/api/v4/leads/{int(lead_id)}"
    payload = {"status_id": int(status_id)}
    headers = {"Authorization": f"Bearer {KOMMO_TOKEN}"}

    log(f"Atualizando lead {lead_id} para etapa '{stage_name}' (status_id={status_id})")
    r = requests.patch(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()


def parse_kommo_form_urlencoded(body: bytes) -> Dict[str, Any]:
    """
    Kommo às vezes envia webhooks como application/x-www-form-urlencoded.
    Aqui transformamos isso em um payload parecido com o JSON padrão.
    """
    text = body.decode("utf-8", "ignore")
    qs = parse_qs(text)

    def first(key: str, default=None):
        vals = qs.get(key)
        return vals[0] if vals else default

    def safe_int(value):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    account = {"subdomain": first("account[subdomain]")}

    message = {"text": first("message[text]")}

    lead_id = safe_int(first("lead[id]"))
    lead = {"id": lead_id} if lead_id is not None else {}

    phone = (
        first("contact[phones][0][value]")
        or first("contact[phones][0][phone]")
        or first("contact[phone]")
        or first("phone")
    )
    contact = {"phones": [{"value": phone}]} if phone else {}

    data: Dict[str, Any] = {"message": message}
    if lead:
        data["lead"] = lead
    if contact:
        data["contact"] = contact

    payload: Dict[str, Any] = {"account": account, "data": data}
    event = first("event")
    if event:
        payload["event"] = event

    return payload


# =========================================
# Helper para chamar a Erika (Assistants API)
# =========================================

def call_openai_erika(
    user_message: str,
    lead_id: Optional[int] = None,
    phone: Optional[str] = None
) -> str:
    """
    Chama a Erika via Assistants API usando o ID configurado em OPENAI_ASSISTANT_ID.
    Retorna o texto bruto da resposta da assistente (incluindo o bloco ERIKA_ACTION).
    """
    if not ERIKA_ASSISTANT_ID:
        raise RuntimeError(
            "OPENAI_ASSISTANT_ID (ID da Erika) não configurado nas variáveis de ambiente."
        )

    meta_parts = []
    if lead_id:
        meta_parts.append(f"lead_id={lead_id}")
    if phone:
        meta_parts.append(f"telefone={phone}")

    meta_text = ""
    if meta_parts:
        meta_text = "[CONTEXTO KOMMO] " + " | ".join(meta_parts)

    messages = [{"role": "user", "content": user_message}]
    if meta_text:
        messages.append({"role": "user", "content": meta_text})

    log("Criando thread para Erika - lead_id:", lead_id, "phone:", phone)

    thread = client.beta.threads.create(messages=messages)

    run = client.beta.threads.runs.create_and_poll(
        thread_id=thread.id,
        assistant_id=ERIKA_ASSISTANT_ID,
    )

    log("Status do run da Erika:", run.status)

    if run.status != "completed":
        raise RuntimeError(
            f"Execução da Erika não completou corretamente. status={run.status}"
        )

    msgs = client.beta.threads.messages.list(thread_id=thread.id, limit=10)

    # Pega a última mensagem da assistente com conteúdo de texto
    for msg in msgs.data:
        if msg.role == "assistant":
            texts = []
            for part in msg.content:
                if part.type == "text":
                    texts.append(part.text.value)
            if texts:
                resposta = "\n\n".join(texts)
                log("Resposta bruta da Erika (primeiros 400 chars):", resposta[:400])
                return resposta

    log("Nenhuma mensagem de assistente encontrada na thread da Erika.")
    return ""


# =========================================
# Webhook Kommo
# =========================================

@app.post("/kommo-webhook")
async def kommo_webhook(request: Request):
    # Lê o corpo uma única vez
    body = await request.body()
    log("Webhook - raw body (primeiros 200 bytes):", body[:200])

    content_type = (request.headers.get("content-type") or "").lower()

    # Tenta decidir se é JSON ou x-www-form-urlencoded
    if "application/json" in content_type:
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception as e:
            log("Erro ao decodificar JSON do webhook:", repr(e))
            raise HTTPException(status_code=400, detail="Payload JSON inválido")
    else:
        try:
            payload = parse_kommo_form_urlencoded(body)
        except Exception as e:
            log("Erro ao decodificar form-url-encoded do webhook:", repr(e))
            raise HTTPException(status_code=400, detail="Payload inválido ou ausente")

    log(
        "Webhook payload normalizado (primeiros 1000 chars):",
        json.dumps(payload, ensure_ascii=False)[:1000],
    )

    # Validação opcional de subdomínio
    if AUTHORIZED_SUBDOMAIN:
        account = payload.get("account") or {}
        subdomain = None
        if isinstance(account, dict):
            subdomain = account.get("subdomain") or account.get("name")
        if subdomain and subdomain != AUTHORIZED_SUBDOMAIN:
            log("Subdomínio não autorizado:", subdomain)
            raise HTTPException(
                status_code=401,
                detail=f"Subdomínio não autorizado: {subdomain}",
            )

    data = payload.get("data") or payload

    # Extração da mensagem de texto
    message_text = (
        (data.get("message") or {}).get("text")
        or (data.get("conversation") or {}).get("last_message", {}).get("text")
        or (data.get("last_message") or {}).get("text")
        or data.get("text")
        or ""
    )

    # Extração do lead_id
    lead = data.get("lead") or {}
    lead_id = (
        lead.get("id")
        or data.get("lead_id")
        or (data.get("conversation") or {}).get("lead_id")
    )

    # Extração de telefone (se vier no payload)
    phone = None
    contact = data.get("contact") or {}
    if isinstance(contact, dict):
        phones = contact.get("phones") or []
        if isinstance(phones, list) and phones:
            first = phones[0]
            if isinstance(first, dict):
                phone = first.get("value") or first.get("phone")
            elif isinstance(first, str):
                phone = first

    if not str(message_text).strip():
        log("Payload sem texto de mensagem. Ignorando.")
        return {
            "status": "ignored",
            "reason": "sem mensagem",
            "payload_keys": list(payload.keys()),
        }

    # Chama a Erika via Assistants API
    try:
        ai_full = call_openai_erika(message_text, lead_id=lead_id, phone=phone)
    except Exception as e:
        log("Erro ao chamar Erika:", repr(e))
        raise HTTPException(
            status_code=500, detail="Erro ao processar resposta da Erika"
        )

    # Separa texto para o cliente e bloco ERIKA_ACTION
    visible_text, action = split_erika_output(ai_full)

    reply_text = (
        visible_text.strip()
        if visible_text and visible_text.strip()
        else "Olá! Sou a Erika, do time TecBrilho 🙂 Como posso cuidar do seu carro hoje?"
    )

    # Cria notas e tenta mover etapa, se possível
    if lead_id:
        try:
            # Nota com a resposta da Erika (sem o bloco técnico)
            add_kommo_note(lead_id, f"Erika 🧠:\n{reply_text}")

            if action and isinstance(action, dict):
                summary = action.get("summary_note")
                if summary:
                    add_kommo_note(lead_id, f"ERIKA_ACTION: {summary}")

                stage = action.get("kommo_suggested_stage")
                if stage:
                    update_lead_stage(lead_id, stage)
        except Exception as e:
            # Não quebra a resposta para o Kommo se der erro na nota/movimentação
            log(
                "Erro ao registrar nota ou atualizar estágio no Kommo:",
                repr(e),
            )

    return {
        "status": "ok",
        "lead_id": lead_id,
        "ai_response": reply_text,
        "erika_action": action,
    }
