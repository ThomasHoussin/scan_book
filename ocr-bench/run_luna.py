# /// script
# requires-python = ">=3.12,<3.15"
# dependencies = ["boto3>=1.40,<2"]
# ///
"""OCR GPT-6 Luna via AWS Bedrock : uv run ocr-bench/run_luna.py [--only ID ...].

Identifiants : chaîne AWS standard (profil `default`, variables d'environnement, SSO).
Écrit runs/luna/<id>.<ext> et <id>.meta.json pour chaque page du manifest.
"""
from __future__ import annotations

import argparse
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

BENCH = Path(__file__).resolve().parent
MODEL_ID = "us.openai.gpt-6-luna"
REGION = "us-east-1"
ENGINE = "luna"
RETRYABLE = {"ThrottlingException", "ServiceUnavailableException", "ModelNotReadyException",
             "InternalServerException", "ModelTimeoutException"}
MAX_ATTEMPTS = 6


def transcribe(client, item: dict, max_tokens: int) -> dict:
    prompt = (BENCH / item["prompt"]).read_text(encoding="utf-8")
    image = (BENCH / item["llm_image"]).read_bytes()
    request = {
        "modelId": MODEL_ID,
        "messages": [{"role": "user", "content": [
            {"image": {"format": "png", "source": {"bytes": image}}},
            {"text": prompt},
        ]}],
        "inferenceConfig": {"maxTokens": max_tokens, "temperature": 0},
    }
    attempt = 0
    while True:
        start = time.perf_counter()
        try:
            response = client.converse(**request)
            break
        except ClientError as exc:
            code = exc.response["Error"]["Code"]
            if code == "ValidationException" and "temperature" in str(exc) and "temperature" in request["inferenceConfig"]:
                del request["inferenceConfig"]["temperature"]  # modèles de raisonnement : température imposée
                continue  # ne compte pas comme une tentative
            if code not in RETRYABLE or attempt == MAX_ATTEMPTS - 1:
                raise
        except BotoCoreError:  # coupure réseau, délai de lecture dépassé…
            if attempt == MAX_ATTEMPTS - 1:
                raise
        time.sleep(2 ** attempt * 5)
        attempt += 1
    seconds = time.perf_counter() - start
    blocks = response["output"]["message"]["content"]
    text = "\n".join(b["text"] for b in blocks if "text" in b).strip()
    return {
        "text": text,
        "meta": {
            "engine": ENGINE,
            "model": MODEL_ID,
            "seconds": round(seconds, 2),
            "latency_ms": response.get("metrics", {}).get("latencyMs"),
            "usage": response.get("usage", {}),
            "stop_reason": response.get("stopReason"),
            "temperature": request["inferenceConfig"].get("temperature"),
            "chars": len(text),
            "status": "ok" if text and response.get("stopReason") == "end_turn" else "suspect",
        },
    }


def run_item(client, item: dict, out_dir: Path, max_tokens: int, overwrite: bool) -> str:
    out = out_dir / f"{item['id']}{item['ext']}"
    meta_path = out_dir / f"{item['id']}.meta.json"
    if out.exists() and meta_path.exists() and not overwrite:
        if json.loads(meta_path.read_text(encoding="utf-8")).get("status") == "ok":
            return f"{item['id']}: déjà fait"  # les pages en échec ou suspectes sont refaites
    try:
        result = transcribe(client, item, max_tokens)
    except Exception as exc:  # une page en échec ne bloque pas les autres
        out.unlink(missing_ok=True)  # pas de sortie périmée notée comme présente
        meta_path.write_text(json.dumps({"engine": ENGINE, "model": MODEL_ID, "status": "error",
                                         "error": f"{type(exc).__name__}: {exc}"}, indent=2), encoding="utf-8")
        return f"{item['id']}: ERREUR {exc}"
    out.write_text(result["text"] + "\n", encoding="utf-8")
    meta_path.write_text(json.dumps(result["meta"], ensure_ascii=False, indent=2), encoding="utf-8")
    meta = result["meta"]
    return f"{item['id']}: {meta['status']} {meta['seconds']}s {meta['chars']} car."


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", nargs="*", help="identifiants de pages à traiter")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, default=16000)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    items = json.loads((BENCH / "manifest.json").read_text(encoding="utf-8"))["items"]
    if args.only:
        items = [i for i in items if i["id"] in set(args.only)]
    out_dir = BENCH / "runs" / ENGINE
    out_dir.mkdir(parents=True, exist_ok=True)
    client = boto3.client("bedrock-runtime", region_name=REGION,
                          config=Config(read_timeout=600, retries={"max_attempts": 2}))
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for line in pool.map(lambda i: run_item(client, i, out_dir, args.max_tokens, args.overwrite), items):
            print(line, flush=True)


if __name__ == "__main__":
    main()
