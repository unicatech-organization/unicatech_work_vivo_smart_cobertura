import base64
import os
import re
import tempfile

import numpy as np
import whisper

try:
    import imageio_ffmpeg
    import shutil
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    ffmpeg_dir = os.path.dirname(ffmpeg_exe)
    ffmpeg_std = os.path.join(ffmpeg_dir, "ffmpeg.exe")
    if not os.path.exists(ffmpeg_std):
        shutil.copy(ffmpeg_exe, ffmpeg_std)
    os.environ["PATH"] += os.pathsep + ffmpeg_dir
except ImportError:
    pass


print("Carregando o modelo de inteligencia artificial Whisper (audio), isso pode demorar alguns segundos...")
model = whisper.load_model("medium")
print("Modelo Whisper carregado com sucesso!")

# O captcha soletra letras (A-Z) e digitos (0-9). Ancorar o Whisper nesse
# alfabeto reduz muito a chance de ele "ouvir" palavras soltas.
_PROMPT_ALFABETO = "A B C D E F G H I J K L M N O P Q R S T U V W X Y Z 0 1 2 3 4 5 6 7 8 9"

# Quando o Whisper transcreve o nome falado em vez do caractere.
_PALAVRA_PARA_CARACTERE = {
    "zero": "0", "um": "1", "hum": "1", "dois": "2", "tres": "3", "três": "3",
    "quatro": "4", "cinco": "5", "seis": "6", "meia": "6", "sete": "7",
    "oito": "8", "nove": "9",
    "agá": "H", "aga": "H", "jota": "J", "til": "", "cedilha": "",
}

# JS rodado dentro da pagina de login: refaz a mesma chamada do botao "ouvir"
# (GET captcha?id=<idCaptcha>) e devolve os clipes base64 (um por caractere).
# Evita gravar o alto-falante (soundcard/WASAPI loopback), que exige aguardar
# a duracao real do audio a cada tentativa e tem vazamento de memoria nativa
# conhecido no soundcard em execucoes longas no Windows.
_JS_BAIXAR_AUDIO = r"""
const callback = arguments[arguments.length - 1];
(async () => {
  try {
    const el = document.querySelector('#idCaptcha');
    if (!el || !el.value) { callback({erro: 'campo #idCaptcha vazio'}); return; }
    const resp = await fetch('captcha?id=' + encodeURIComponent(el.value),
                             {credentials: 'include', cache: 'no-store'});
    if (!resp.ok) { callback({erro: 'HTTP ' + resp.status}); return; }
    const json = await resp.json();
    const arr = Array.isArray(json.base64) ? json.base64 : [json.base64];
    callback({clipes: arr});
  } catch (e) {
    callback({erro: String(e)});
  }
})();
"""


def _normalizar(texto):
    """Converte a transcricao bruta do Whisper na string de caracteres do captcha."""
    tokens = re.split(r"[\s,.;:!?/-]+", texto.strip().lower())
    saida = []
    for tok in tokens:
        if not tok:
            continue
        if tok in _PALAVRA_PARA_CARACTERE:
            saida.append(_PALAVRA_PARA_CARACTERE[tok])
        else:
            saida.append(re.sub(r"[^a-z0-9]", "", tok))
    return "".join(saida).lower()


def _transcrever_wavs(caminhos):
    """Junta os audios (com silencio entre eles) e transcreve de uma vez.

    Transcrever o conjunto de uma vez e bem mais confiavel do que clipe a clipe:
    clipes de ~0.5s isolados fazem o Whisper alucinar.
    """
    silencio = np.zeros(int(16000 * 0.5), dtype=np.float32)
    partes = [silencio]
    for caminho in caminhos:
        partes.append(whisper.load_audio(caminho))
        partes.append(silencio)
    audio = np.concatenate(partes)

    result = model.transcribe(
        audio,
        fp16=False,
        language="pt",
        temperature=0,
        beam_size=5,
        initial_prompt=_PROMPT_ALFABETO,
    )
    return result["text"].strip()


def baixar_audio_captcha(driver):
    """Baixa os clipes de audio do captcha pelo proprio navegador logado.

    Retorna lista de bytes (um MP3 por caractere). Lanca RuntimeError em falha.
    """
    driver.set_script_timeout(20)
    resposta = driver.execute_async_script(_JS_BAIXAR_AUDIO)
    if not resposta or resposta.get("erro"):
        raise RuntimeError(f"Falha ao baixar audio do captcha: {(resposta or {}).get('erro')}")

    clipes = resposta.get("clipes") or []
    clipes = [base64.b64decode(c) for c in clipes if c]
    if not clipes:
        raise RuntimeError("Endpoint do captcha nao devolveu nenhum clipe de audio")
    return clipes


def transcrever_captcha(driver):
    """Baixa e transcreve o audio do captcha atual. Retorna a string ja normalizada."""
    clipes = baixar_audio_captcha(driver)

    with tempfile.TemporaryDirectory(prefix="captcha_audio_") as tmp:
        caminhos = []
        for i, dados in enumerate(clipes):
            p = os.path.join(tmp, f"{i:02d}.mp3")
            with open(p, "wb") as f:
                f.write(dados)
            caminhos.append(p)
        bruto = _transcrever_wavs(caminhos)

    return _normalizar(bruto)
