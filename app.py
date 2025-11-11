import os
import json
import datetime
from typing import Optional, Tuple, Dict, Any, List

from urllib.parse import parse_qs

import requests
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import JSONResponse
from openai import OpenAI

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


# =========================================
# Helpers extra para REATIVAÇÃO (Kommo)
# =========================================

def fetch_leads_for_reactivation(limit: int = 5) -> List[Dict[str, Any]]:
    """
    Busca no Kommo alguns leads candidatos a reativação.
    Aqui usamos, por exemplo, a etapa "Reengajar".
    Ajuste este filtro conforme sua estratégia.
    """
    if not KOMMO_DOMAIN or not KOMMO_TOKEN:
        log("fetch_leads_for_reactivation: KOMMO_DOMAIN/KOMMO_TOKEN não configurados.")
        return []

    status_id_reengajar = os.getenv("KOMMO_STATUS_REENGAJAR")
    if not status_id_reengajar:
        log("fetch_leads_for_reactivation: KOMMO_STATUS_REENGAJAR não configurado.")
        return []

    url = f"{KOMMO_DOMAIN}/api/v4/leads"
    headers = {"Authorization": f"Bearer {KOMMO_TOKEN}"}
    params = {
        "limit": limit,
        "filter[statuses][0][status_id]": int(status_id_reengajar),
    }

    log("Buscando leads para reativação em", url, "params=", params)
    r = requests.get(url, headers=headers, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()

    embedded = data.get("_embedded", {})
    leads = embedded.get("leads", [])
    log(f"fetch_leads_for_reactivation: encontrados {len(leads)} leads candidatos.")
    return leads


def build_lead_context_text(lead: Dict[str, Any]) -> str:
    """
    Monta um texto de contexto simples para mandar à Erika no modo reativação.
    Você pode enriquecer isso depois com histórico completo, notas, etc.
    """
    lead_id = lead.get("id")
    name = lead.get("name") or "Cliente"
    status_id = lead.get("status_id")
    price = lead.get("price")
    created_at = lead.get("created_at")
    updated_at = lead.get("updated_at")

    lines = [
        f"Lead ID: {lead_id}",
        f"Nome exibido do lead: {name}",
        f"Status_id atual no Kommo: {status_id}",
        f"Valor (se houver): {price}",
        f"Criado em (timestamp): {created_at}",
        f"Última atualização (timestamp): {updated_at}",
    ]

    context_text = "\n".join(lines)
    return context_text


def send_whatsapp_via_kommo(lead: Dict[str, Any], text: str):
    """
    ENVIO REAL DE MENSAGEM VIA KOMMO (WHATSAPP).

    ATENÇÃO:
      - Este é um placeholder em modo SEGURO.
      - Neste momento, ele APENAS cria uma nota no lead com o texto
        que seria enviado no WhatsApp.
      - Quando você tiver o endpoint exato de envio via Kommo,
        substitua este corpo pelo POST correto.

    Ideia futura:
      - Pegar o contact_id / chat_id vinculado ao lead
      - Usar o endpoint oficial de mensagens/chats da API Kommo
      - Mandar 'text' pro WhatsApp do cliente.
    """
    lead_id = lead.get("id")
    if not lead_id:
        log("send_whatsapp_via_kommo: lead sem id, não foi possível enviar.")
        return

    nota = f"[MENSAGEM PARA WHATSAPP]\n{text}"
    try:
        add_kommo_note(lead_id, nota)
        log(f"send_whatsapp_via_kommo: nota criada com texto de WhatsApp para lead {lead_id}.")
    except Exception as e:
        log("send_whatsapp_via_kommo: erro ao criar nota simulando mensagem:", repr(e))


# =========================================
# Helpers para normalizar payload do Kommo
# =========================================

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

    # ---------------------------
    # Texto da mensagem
    # ---------------------------
    # Tentamos pegar o campo "bonito" primeiro
    msg_text = (
        first("message[text]")
        or first("message[body]")
        or first("message[message]")
        or first("message[add][0][text]")
        or first("message[add][0][message]")
    )

    # Vamos montar o dicionário message com QUALQUER campo message[...]
    message: Dict[str, Any] = {}
    for key, vals in qs.items():
        if key.startswith("message[") and key.endswith("]"):
            inner = key[len("message["):-1]  # pega o que estiver entre [ ]
            if vals:
                message[inner] = vals[0]

    # Se ainda não achamos o texto, vasculhamos as chaves
    if not msg_text and message:
        for k, v in message.items():
            k_str = str(k)
            if "text" in k_str or "message" in k_str or "body" in k_str:
                msg_text = v
                break

    # Se achamos algum texto, garantimos um campo canonical "text"
    if msg_text:
        message["text"] = msg_text

    # ---------------------------
    # Lead id
    # ---------------------------
    lead_id = safe_int(
        first("lead[id]")
        or first("leads[0][id]")
        or first("message[add][0][entity_id]")
        or first("message[add][0][element_id]")
    )

    lead = {"id": lead_id} if lead_id is not None else {}

    # ---------------------------
    # Telefone
    # ---------------------------
    phone = (
        first("contact[phones][0][value]")
        or first("contact[phones][0][phone]")
        or first("contact[phone]")
        or first("phone")
    )
    contact = {"phones": [{"value": phone}]} if phone else {}

    data: Dict[str, Any] = {}
    if message:
        data["message"] = message
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

def call_openai_erika(user_message: str,
                      lead_id: Optional[int] = None,
                      phone: Optional[str] = None) -> str:
    """
    Chama a Erika via Assistants API usando o ID configurado em OPENAI_ASSISTANT_ID.
    Retorna o texto bruto da resposta da assistente (incluindo o bloco ERIKA_ACTION).
    """
    if not ERIKA_ASSISTANT_ID:
        raise RuntimeError("OPENAI_ASSISTANT_ID (ID da Erika) não configurado nas variáveis de ambiente.")

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
        raise RuntimeError(f"Execução da Erika não completou corretamente. status={run.status}")

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
    # Lê o corpo bruto para poder tratar JSON ou x-www-form-urlencoded
    raw_body = await request.body()
    log("Webhook - raw body (primeiros 200 bytes):", raw_body[:200])

    content_type = (request.headers.get("content-type") or "").lower()

    # Tenta normalizar o payload dependendo do content-type
    try:
        if "application/json" in content_type:
            payload = json.loads(raw_body.decode("utf-8"))
        elif "application/x-www-form-urlencoded" in content_type:
            payload = parse_kommo_form_urlencoded(raw_body)
        else:
            # Fallback: tenta JSON primeiro
            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except Exception:
                # Último recurso: trata como form-url-encoded
                payload = parse_kommo_form_urlencoded(raw_body)
    except Exception as e:
        log("Erro ao normalizar payload do webhook:", repr(e))
        raise HTTPException(status_code=400, detail="Payload inválido ou ausente")

    log("Webhook payload normalizado (primeiros 1000 chars):", json.dumps(payload)[:1000])

    # Validação opcional de subdomínio
    if AUTHORIZED_SUBDOMAIN:
        account = payload.get("account") or {}
        subdomain = None
        if isinstance(account, dict):
            subdomain = account.get("subdomain") or account.get("name")
        if subdomain and subdomain != AUTHORIZED_SUBDOMAIN:
            log("Subdomínio não autorizado:", subdomain)
            raise HTTPException(status_code=401, detail=f"Subdomínio não autorizado: {subdomain}")

    data = payload.get("data") or payload

    # Extração da mensagem de texto (mais flexível)
    msg_block = data.get("message") or {}
    message_text = (
        msg_block.get("text")        # formato comum em JSON ou setado pelo parser
        or msg_block.get("body")     # alguns webhooks usam "body"
        or msg_block.get("message")  # fallback genérico
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

    # Se ainda não achou lead_id, tenta extrair de message[add][0][entity_id]/element_id
    if not lead_id and isinstance(msg_block, dict):
        for k, v in msg_block.items():
            k_str = str(k)
            if "entity_id" in k_str or "element_id" in k_str:
                try:
                    lead_id = int(v)
                    break
                except (TypeError, ValueError):
                    continue

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
        raise HTTPException(status_code=500, detail="Erro ao processar resposta da Erika")

    # Separa texto para o cliente e bloco ERIKA_ACTION
    visible_text, action = split_erika_output(ai_full)

    reply_text = (
        visible_text.strip()
        if visible_text and visible_text.strip()
        else "Oi! Sou a Erika, da TecBrilho. Como posso te ajudar hoje?"
    )

    # Cria notas e tenta mover etapa, se possível
    if lead_id:
        try:
            # Nota com a resposta da Erika
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
            log("Erro ao registrar nota ou atualizar estágio no Kommo:", repr(e))

    return JSONResponse(
        {
            "status": "ok",
            "lead_id": lead_id,
            "ai_response": reply_text,
            "erika_action": action,
        }
    )


# =========================================
# Endpoint CRON de REATIVAÇÃO
# =========================================

@app.post("/cron/reactivar")
async def cron_reactivar(x_cron_key: Optional[str] = Header(None)):
    """
    Endpoint chamado por um CRON EXTERNO para reativar leads antigos.
    Protegido por header:  X-CRON-KEY: <CRON_SECRET>
    """
    secret = os.getenv("CRON_SECRET") or ""
    if not secret:
        log("cron_reactivar: CRON_SECRET não configurado nas variáveis de ambiente.")
        raise HTTPException(status_code=500, detail="CRON_SECRET não configurado")

    if not x_cron_key or x_cron_key != secret:
        log("cron_reactivar: chave de cron inválida ou ausente.")
        raise HTTPException(status_code=401, detail="Não autorizado")

    # 1) Buscar alguns leads candidatos
    try:
        leads = fetch_leads_for_reactivation(limit=5)
    except Exception as e:
        log("cron_reactivar: erro ao buscar leads:", repr(e))
        raise HTTPException(status_code=500, detail="Erro ao buscar leads para reativação")

    if not leads:
        log("cron_reactivar: nenhum lead candidato encontrado.")
        return {"status": "ok", "processed": 0, "details": []}

    detalhes: List[Dict[str, Any]] = []

    for lead in leads:
        lead_id = lead.get("id")
        if not lead_id:
            continue

        try:
            context_text = build_lead_context_text(lead)

            # Mensagem especial para o modo reativação
            system_instructions = (
                "Erika, agora você está em MODO REATIVAÇÃO DE LEADS.\n\n"
                "Você receberá dados de um lead TecBrilho + um pequeno contexto.\n"
                "Suas tarefas para ESTE lead específico são:\n"
                "1) Decidir se vale a pena reativar agora.\n"
                "2) Se sim, escrever uma mensagem humana e gentil que será enviada via WhatsApp,\n"
                "   retomando a conversa de forma natural.\n"
                "3) SEMPRE devolver no final um bloco ERIKA_ACTION neste formato:\n"
                "### ERIKA_ACTION\n"
                "{\n"
                '  \"should_reactivate\": true ou false,\n'
                '  \"kommo_suggested_stage\": \"Reengajar\" ou outra etapa válida,\n'
                '  \"summary_note\": \"Resumo curto da sua decisão.\"\n'
                "}\n"
                "### END_ERIKA_ACTION\n"
            )

            user_message = (
                f"{system_instructions}\n\n"
                "A seguir estão os dados e contexto do lead:\n\n"
                f"{context_text}\n\n"
                "Com base nisso, aja conforme as instruções acima."
            )

            # Chama a Erika com esse contexto
            raw_response = call_openai_erika(user_message, lead_id=lead_id)
            visible_text, action = split_erika_output(raw_response)

            visible_text = visible_text.strip() if visible_text else ""
            if not visible_text:
                visible_text = (
                    "Oi, tudo bem? Aqui é a Erika, da TecBrilho. "
                    "Passei pra saber se ainda faz sentido pra você cuidar daquele serviço no seu carro. 🙂"
                )

            should_reactivate = False
            suggested_stage = None
            summary_note = None

            if action and isinstance(action, dict):
                should_reactivate = bool(action.get("should_reactivate"))
                suggested_stage = action.get("kommo_suggested_stage")
                summary_note = action.get("summary_note")

            # Nota com decisão da Erika
            if summary_note:
                try:
                    add_kommo_note(lead_id, f"[ERIKA REATIVAÇÃO]\n{summary_note}")
                except Exception as e:
                    log("cron_reactivar: erro ao criar nota de summary_note:", repr(e))

            # Se ela decidiu reativar, "enviar" mensagem + mover etapa se sugerido
            if should_reactivate:
                send_whatsapp_via_kommo(lead, visible_text)

                if suggested_stage:
                    try:
                        update_lead_stage(lead_id, suggested_stage)
                    except Exception as e:
                        log("cron_reactivar: erro ao atualizar etapa do lead:", repr(e))

            detalhes.append(
                {
                    "lead_id": lead_id,
                    "should_reactivate": should_reactivate,
                    "suggested_stage": suggested_stage,
                    "summary_note": summary_note,
                }
            )

        except Exception as e:
            log(f"cron_reactivar: erro ao processar lead {lead_id}:", repr(e))
            detalhes.append(
                {
                    "lead_id": lead_id,
                    "error": str(e),
                }
            )

    return {
        "status": "ok",
        "processed": len(detalhes),
        "details": detalhes,
    }
