"""Explicit intranet text adapter using the existing bounded/TLS/allowlist transport."""
import json
import time

from ..embedding_provider import OpenAICompatProvider, ProviderError
from .protocol import ExecutorFailure
from .executor import CommandExecutorRunner


class IntranetTextRunner:
    def __init__(self, options):
        if options.get('protocol') != 'openai_chat_completions':
            raise ExecutorFailure('protocol_unverified')
        if any(key in options for key in ('api_key', 'token', 'password', 'secret')):
            raise ExecutorFailure('invalid_config')
        self.options = options
        self.transport = OpenAICompatProvider({**options, 'api_root': options.get('endpoint'),
                                               'secret_ref': options.get('secret_ref')})

    def run_text(self, request, full_text, segments=None, *, instructions=None):
        payload = {'job_id': request.job_id, 'scope': request.scope, 'materials': full_text,
                   'segments': segments or [],
                   'instructions': instructions or CommandExecutorRunner.PROMPT_INSTRUCTIONS}
        # 推理型模型的 reasoning tokens 计入 completion 并先于正文消耗预算：
        # 上限须容纳"思考+结构化候选输出"（vLLM 网关实测），超时对齐命令执行器 300s。
        seconds = max(.05, min(300, float(self.options.get('timeout_seconds', 120))))
        try:
            data = self.transport._request('/chat/completions', {
                'model': self.transport.model, 'temperature': 0,
                'messages': [{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}],
                'response_format': {'type': 'json_object'},
                'max_tokens': max(256, min(32768, int(self.options.get('max_output_tokens', 4096))))},
                deadline=time.monotonic()+seconds, timeout_budget=seconds)
            # 网关常对模型名归一化回显（如去命名空间前缀/纠正别名），与请求名永不相等。
            # response_model 是选择式的期望回显（与 embedding provider 同语义）；缺省退回请求名。
            expected_model = self.options.get('response_model') or self.transport.model
            if data.get('model') and data['model'] != expected_model:
                raise ExecutorFailure('model_mismatch')
            result = json.loads(data['choices'][0]['message']['content'])
            if not isinstance(result, dict): raise ValueError()
            usage = data.get('usage')
            if isinstance(usage, dict):
                result['usage'] = {k: v for k,v in usage.items() if k in
                    {'prompt_tokens', 'completion_tokens', 'total_tokens'} and type(v) is int and v >= 0}
            return result
        except ProviderError as exc:
            raise ExecutorFailure(exc.category) from None
        except (KeyError, IndexError, ValueError, TypeError):
            raise ExecutorFailure('structured_output') from None
