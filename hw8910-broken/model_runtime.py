"""Shared inference path for full evaluation and FastAPI."""
import importlib.metadata
import hashlib
import os
import re
from pathlib import Path

from release import file_digest, read, validate_release, verify_files

PACKAGES = ('torch', 'transformers', 'peft', 'tokenizers')


def versions():
    return {name: importlib.metadata.version(name) for name in PACKAGES}


def snapshot(runtime):
    from huggingface_hub import snapshot_download
    if not re.fullmatch('[0-9a-f]{40}', runtime['base_revision']):
        raise ValueError('Нужна точная ревизия базы')
    return Path(snapshot_download(runtime['base_name'], revision=runtime['base_revision'],
                                  local_files_only=True))


def template_digest(path):
    external = path / 'chat_template.jinja'
    if external.exists():
        return file_digest(external)
    value = read(path / 'tokenizer_config.json')['chat_template']
    return hashlib.sha256(value.encode()).hexdigest()


def artifact_root(m, root):
    if m['kind'] == 'base':
        if m['artifact_uri'] != f"hf://{m['runtime']['base_name']}@{m['runtime']['base_revision']}":
            raise ValueError('URI базы не соответствует ревизии')
        return snapshot(m['runtime'])
    if m['kind'] != 'adapter' or m['artifact_uri'] != f"runs:/{m['training_run_id']}/model":
        raise ValueError('Неизвестный артефакт')
    return root / 'models' / 'adapter'


def verify_runtime(m, root):
    runtime = m['runtime']
    base = snapshot(runtime)
    artifact = artifact_root(m, root)
    verify_files(m, artifact)
    expected = {p.name: file_digest(p) for p in sorted(base.iterdir()) if p.is_file()}
    if expected != runtime['base_file_hashes']:
        raise ValueError('Файлы базовой модели изменены')
    actual = {p.name: file_digest(p) for p in sorted(artifact.iterdir()) if p.is_file()}
    if actual != m['file_hashes'] or versions() != runtime['packages']:
        raise ValueError('Состав артефакта или версии runtime изменены')
    if (actual['tokenizer.json'] != runtime['tokenizer_sha256']
            or template_digest(artifact) != runtime['template_sha256']
            or file_digest(__file__) != runtime['inference_code_sha256']):
        raise ValueError('Токенизатор, шаблон или код генерации изменены')
    for key in ('evaluation_report', 'raw_outputs', 'release_report'):
        if key not in m:
            raise ValueError('Нет связанного отчёта: ' + key)
        name, expected = m[key]['path'], m[key]['sha256']
        path = Path(name)
        if path.is_absolute() or '..' in path.parts or file_digest(root / path) != expected:
            raise ValueError('Отчёт изменён: ' + key)


class TransformersBackend:
    def __init__(self, m, root):
        os.environ.setdefault('PYTORCH_MPS_HIGH_WATERMARK_RATIO', '0.5')
        os.environ.setdefault('PYTORCH_MPS_LOW_WATERMARK_RATIO', '0.4')
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.torch = torch
        self.runtime = r = m['runtime']
        self.device = r['device']
        self.loaded_id = m['model_id']
        self.tokenizer = AutoTokenizer.from_pretrained(artifact_root(m, root), local_files_only=True)
        model = AutoModelForCausalLM.from_pretrained(snapshot(r), local_files_only=True,
                                                    dtype=getattr(torch, r['dtype']))
        if m['kind'] == 'adapter':
            from peft import PeftModel
            model = PeftModel.from_pretrained(model, artifact_root(m, root))
        self.model = model.to(self.device).eval()
        torch.manual_seed(r['generation']['seed'])

    def generate(self, prompt, limit):
        g = self.runtime['generation']
        if limit != g['max_new_tokens']:
            raise ValueError('Лимит вне протокола')
        text = self.tokenizer.apply_chat_template(
            [{'role': 'system', 'content': g['system']}, {'role': 'user', 'content': prompt}],
            tokenize=False, add_generation_prompt=True, enable_thinking=g['enable_thinking'])
        batch = self.tokenizer(text, return_tensors='pt')
        n = batch['input_ids'].shape[1]
        if n > self.runtime['max_input_tokens']:
            raise ValueError('Вход слишком длинный после токенизации')
        batch = batch.to(self.device)
        with self.torch.inference_mode():
            out = self.model.generate(**batch, max_new_tokens=limit, do_sample=g['do_sample'],
                                      pad_token_id=self.tokenizer.pad_token_id or self.tokenizer.eos_token_id)
        tokens = out[0, n:]
        return dict(answer=self.tokenizer.decode(tokens, skip_special_tokens=True),
                    output_tokens=int(tokens.numel()))
