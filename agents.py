"""
agents.py
---------
Bir "agent" = model (provider) + rol talimatı (system_prompt).

Burada rollerin TALİMATLARINI (ROLE_PROMPTS) sabit tutuyoruz ama hangi rolün
hangi modeli kullanacağını dışarıdan (arayüzden) seçebiliyoruz. Bu, #4
"model yönlendirme (routing)" optimizasyonudur: role göre model atamak.
"""

from adapters import PROVIDERS, LLMResponse

# Her rolün talimatı (system prompt). Modeli sabit DEĞİL — çalışırken atanır.
ROLE_PROMPTS = {
    "planner": (
        "Sen deneyimli bir yazılım mimarısın. Sana verilen görevi net, numaralı "
        "adımlara böl. Kod YAZMA — sadece nasıl yapılacağının kısa, uygulanabilir "
        "planını çıkar."
    ),
    "coder": (
        "Sen usta bir yazılım geliştiricisin. Sana verilen planı ve (varsa) "
        "inceleme/çalıştırma geri bildirimini uygulayan, çalışan ve temiz kod yaz. "
        "Cevabında SADECE kodu tek bir kod bloğu içinde ver, açıklama ekleme."
    ),
    "reviewer": (
        "Sen titiz bir kod inceleyicisin. Verilen kodu hata, eksik durum ve kötü "
        "uygulamalar açısından incele. Kod iyiyse cevabına SADECE 'VERDICT: APPROVED' "
        "yaz. Düzeltme gerekiyorsa önce 'VERDICT: NEEDS_FIX' yaz, sonra maddeler "
        "halinde tam olarak neyin düzeltilmesi gerektiğini açıkla."
    ),
}

# Varsayılan rol -> model ataması (arayüzden değiştirilebilir).
DEFAULT_ROUTING = {
    "planner": "claude",     # muhakeme
    "coder": "deepseek",     # kod (ucuz + güçlü)
    "reviewer": "gemini",    # hızlı ikinci göz
}


class Agent:
    def __init__(self, role: str, provider: str):
        self.role = role
        self.provider = provider
        self.system_prompt = ROLE_PROMPTS[role]

    def run(self, user_prompt: str) -> LLMResponse:
        return PROVIDERS[self.provider](self.system_prompt, user_prompt)


def build_agents(routing: dict | None = None) -> dict:
    """routing = {'planner':'claude', 'coder':'deepseek', 'reviewer':'gemini'}"""
    routing = {**DEFAULT_ROUTING, **(routing or {})}
    return {role: Agent(role, provider) for role, provider in routing.items()}
