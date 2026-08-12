"""Sugestão de texto alternativo (alt) por IA — Claude Haiku.

A UI manda uma versão REDUZIDA da imagem (≤512px, JPEG — o navegador encolhe
no canvas antes de subir), o que mantém o custo em ~US$ 0,001/imagem no Haiku
(tokens de imagem ≈ largura×altura/750). A sugestão preenche o campo Alt e
continua editável — a pessoa revisa antes de publicar.

Provider é detalhe de implementação: troque suggest_alt() e o resto da app
não muda. Precisa de ANTHROPIC_API_KEY (Secret Manager em produção, .env
local); sem a chave o /api/alt-suggest responde 503 e a UI esconde o botão ✨.
"""
from __future__ import annotations

import base64

from config import Config

MODEL = "claude-haiku-4-5"
PROMPT = (
    "Escreva o texto alternativo (alt) desta imagem em português, para "
    "leitores de tela: uma frase objetiva de 5 a 25 palavras. É a imagem de "
    "um anúncio de passeio de bicicleta. Se a imagem contiver texto (cartaz), "
    "transcreva o essencial. Não comece com 'imagem de' ou 'foto de'; sem "
    "aspas nem emoji. Responda somente com o texto alternativo."
)


def suggest_alt(data: bytes, mime: str = "image/jpeg") -> str:
    """Descreve a imagem com o Haiku e devolve o alt sugerido (1 frase)."""
    if not Config.ANTHROPIC_API_KEY:
        raise RuntimeError("ANTHROPIC_API_KEY não definido — sugestão de alt desativada.")
    import anthropic  # import tardio: a app sobe mesmo sem o pacote/chave

    client = anthropic.Anthropic(api_key=Config.ANTHROPIC_API_KEY)
    response = client.messages.create(
        model=MODEL,
        max_tokens=200,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": mime or "image/jpeg",
                            "data": base64.standard_b64encode(data).decode("ascii")}},
                {"type": "text", "text": PROMPT},
            ],
        }],
    )
    text = next((b.text for b in response.content if b.type == "text"), "")
    text = text.strip().strip('"').strip()
    # Transparência: alt gerado por IA leva o crédito — no código, não no
    # prompt, pra sair SEMPRE (a pessoa pode apagar ao editar, se quiser).
    return f"{text} (gerado por Claude Haiku 4.5)" if text else text
