"""SageMaker Batch Transform protocol; one JSON request per line."""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aws.jobs import load_bundle, series_key
from retail_forecast.forecast import predict_artifact
from retail_forecast.storage import build_forecast_payload


def infer(bundle, request):
    dataset = bundle["dataset"]
    store, item, model = request["store_id"], request["item_id"], request["model"]
    artifact = bundle["artifacts"][series_key(store, item)][model]
    payload = build_forecast_payload(dataset, store, item, dataset["cutoff"], model, analysis=predict_artifact(artifact))
    payload.pop("_actuals", None)
    return payload


def serve():
    bundle = load_bundle(os.environ.get("SM_MODEL_DIR", "/opt/ml/model"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/ping" and bundle.get("artifacts") else 404)
            self.end_headers()

        def do_POST(self):
            if self.path != "/invocations":
                self.send_error(404)
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if size <= 0 or size > 6 * 1024 * 1024:
                    raise ValueError("Invalid payload length")
                requests = self.rfile.read(size).decode("utf-8").splitlines()
                body = "\n".join(json.dumps(infer(bundle, json.loads(line)), allow_nan=False)
                                 for line in requests if line.strip()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/jsonlines")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ValueError, KeyError, TypeError) as exc:
                self.send_error(400, str(exc))

    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
