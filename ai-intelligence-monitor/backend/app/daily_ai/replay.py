import hashlib
import json


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


def frozen_source_replay(source_bundle: dict, replay_bundle: dict) -> dict:
    return {"stable": _digest(source_bundle) == _digest(replay_bundle), "network_requests": 0, "search_calls": 0, "deepseek_calls": 0}


def frozen_output_replay(source_outputs: list[dict], replay_outputs: list[dict]) -> dict:
    return {"stable": _digest(source_outputs) == _digest(replay_outputs), "network_requests": 0, "deepseek_calls": 0}
