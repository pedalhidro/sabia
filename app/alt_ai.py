"""Sugestão de texto alternativo (alt) por IA — Claude Opus 5, com thinking.

A UI manda uma versão REDUZIDA da imagem (≤512px, JPEG — o navegador encolhe
no canvas antes de subir). No Opus 5 o thinking vem ligado por padrão: o
modelo olha a imagem com calma antes de escrever — o "carinho" custa
~US$ 0,01/imagem (tokens de imagem ≈ largura×altura/750 + o pensamento).
A sugestão preenche o campo Alt e continua editável — a pessoa revisa antes
de publicar. Pra economizar, MODEL = "claude-haiku-4-5" (~US$0,001/imagem);
pro máximo, "claude-fable-5" (thinking sempre ligado, ~US$0,02).

Provider é detalhe de implementação: troque suggest_alt() e o resto da app
não muda. Precisa de ANTHROPIC_API_KEY (Secret Manager em produção, .env
local); sem a chave o /api/alt-suggest responde 503 e a UI esconde o botão ✨.
"""
from __future__ import annotations

import base64

from config import Config

MODEL = "claude-opus-5"
MODEL_LABEL = "Claude Opus 5"
PROMPT = (
    "Escreva o texto alternativo (alt) desta imagem em português, para "
    "leitores de tela: uma frase objetiva de 5 a 25 palavras (no máximo 450 "
    "caracteres). É a imagem de um anúncio de passeio de bicicleta. Se a "
    "imagem contiver texto (cartaz), transcreva o essencial. Não comece com "
    "'imagem de' ou 'foto de'; sem aspas nem emoji. Responda somente com o "
    "texto alternativo."
)


def suggest_alt(data: bytes, mime: str = "image/jpeg", context: str = "") -> str:
    """Descreve a imagem com o Haiku e devolve o alt sugerido (1 frase).
    context = texto do anúncio (legenda/blocos): ajuda a NOMEAR o que aparece
    (passeio, praça, córrego) — mas o alt descreve o que se VÊ, sem repetir a
    legenda (leitores de tela leem as duas coisas; alt redundante atrapalha)."""
    if not Config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY não definido — sugestão de alt desativada.")
    import anthropic  # import tardio: a app sobe mesmo sem o pacote/chave

    prompt = PROMPT
    if context.strip():
        prompt += ("\n\nContexto (o texto do anúncio que acompanha a imagem — "
                   "use só pra identificar nomes/lugares do que aparece na "
                   "imagem; NÃO repita o texto do anúncio no alt):\n"
                   + context.strip()[:1000])

    client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=MODEL,
        # max_tokens cobre PENSAMENTO + resposta (no Opus 5 o thinking é
        # padrão e conta aqui) — apertado demais truncaria a frase final.
        max_tokens=3000,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": mime or "image/jpeg",
                            "data": base64.standard_b64encode(data).decode("ascii")}},
                {"type": "text", "text": prompt},
            ],
        }],
    )
    # Opus 5/Fable têm classificadores que podem recusar (stop_reason em vez
    # de erro HTTP) — improvável em foto de pedal, mas checa antes de ler.
    if response.stop_reason == "refusal":
        raise RuntimeError("o modelo recusou descrever esta imagem")
    text = next((b.text for b in response.content if b.type == "text"), "")
    text = text.strip().strip('"').strip()
    # Transparência: alt gerado por IA leva o crédito — no código, não no
    # prompt, pra sair SEMPRE (a pessoa pode apagar ao editar, se quiser).
    return f"{text} (gerado por {MODEL_LABEL})" if text else text
