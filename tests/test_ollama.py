import json
from io import BytesIO

import pytest
from PIL import Image

from qianniu.ollama import Ollama, ModelFailure


class Response:
    def __init__(self, data):
        self.payload = json.dumps(data).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self):
        return self.payload


def test_unreachable_model_fails_boundedly():
    calls = []

    def unavailable(request, timeout):
        calls.append(request)
        raise OSError("unreachable")

    with pytest.raises(ModelFailure):
        Ollama({"retries": 2}, opener=unavailable).probe()
    assert len(calls) == 3


def test_title_source_ids_resolve_only_to_supplied_sources():
    result = {'title': 'candidate', 'guide_title': 'candidate', 'sources': ['F1'], 'missing': []}
    m = Ollama({'retries': 0}, opener=lambda *a, **k: Response({'done': True, 'response': json.dumps(result)}))
    advice = m.suggest([{'name': '材质', 'value': '紫铜', 'source': 'local supplier photo', 'confirmed': True}])
    assert advice['sources'] == ['local supplier photo']
    assert advice['confirmed'] is False


def test_invalid_json_never_becomes_facts(tmp_path):
    image = tmp_path / "sku.png"
    Image.new("RGB", (2, 2)).save(image)
    with pytest.raises(ModelFailure):
        Ollama({"retries": 0}, opener=lambda *a, **k: Response({"done": True, "response": "not json"})).extract(image)


def test_extraction_source_is_bound_to_supplied_image_and_unconfirmed(tmp_path):
    image = tmp_path / "sku.png"
    Image.new("RGB", (2, 2)).save(image)
    model = {"facts": [{"name": "model", "value": "JG6-6", "source": str(image.resolve()), "evidence_text": "JG6-6"}]}
    request_data = []

    def served(req, timeout):
        request_data.append(json.loads(req.data))
        return Response({"done": True, "response": json.dumps(model)})

    result = Ollama({"retries": 0}, opener=served).extract(image)
    assert result["facts"][0]["confirmed"] is False
    assert result["facts"][0]["source"] == str(image.resolve())
    assert request_data[0]["think"] is False and len(request_data[0]["images"]) == 1


def test_fabricated_source_is_rejected(tmp_path):
    image = tmp_path / "sku.png"
    Image.new("RGB", (2, 2)).save(image)
    result = {"facts": [{"name": "voltage", "value": "600V", "source": "not supplied", "evidence_text": "600V"}]}
    with pytest.raises(ModelFailure):
        Ollama({"retries": 0}, opener=lambda *a, **k: Response({"done": True, "response": json.dumps(result)})).extract(image)


def test_missing_model_does_not_claim_connected():
    with pytest.raises(ModelFailure):
        Ollama({"retries": 0}, opener=lambda *a, **k: Response({"models": []})).probe()


def test_incomplete_generation_is_failure(tmp_path):
    image = tmp_path / "sku.png"
    Image.new("RGB", (2, 2)).save(image)
    with pytest.raises(ModelFailure):
        Ollama({"retries": 0}, opener=lambda *a, **k: Response({"done": False, "response": '{"facts":[]}'})).extract(image)
