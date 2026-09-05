import type { Env } from "./types";

export type ChatMessage = { role: string; content: string };

export type ChatResult = {
  answer: string;
  provider: string;
  model: string;
  status: "OK" | "LOCAL_FALLBACK" | "NOT_CONFIGURED" | "ERROR";
};

const EXTERNAL_PROVIDERS = new Set(["chatgpt", "gemini", "deepseek", "claude"]);

function externalDisplayName(provider: string): string {
  switch (provider) {
    case "chatgpt":
      return "ChatGPT";
    case "gemini":
      return "Gemini";
    case "deepseek":
      return "DeepSeek";
    case "claude":
      return "Claude";
    default:
      return provider;
  }
}

function isWeatherQuestion(text: string): boolean {
  const t = text.toLowerCase();
  return (
    t.includes("طقس") ||
    t.includes("الجو") ||
    t.includes("حرارة") ||
    t.includes("درجة الحرارة") ||
    t.includes("كم الحرارة") ||
    t.includes("الطقس") ||
    t.includes("ممطر") ||
    t.includes("weather") ||
    t.includes("temperature") ||
    t.includes("forecast")
  );
}

type City = { name: string; lat: number; lon: number };

const CITIES: City[] = [
  { name: "بغداد", lat: 33.3152, lon: 44.3661 },
  { name: "أربيل", lat: 36.1911, lon: 44.0093 },
  { name: "البصرة", lat: 30.5085, lon: 47.7804 },
  { name: "الموصل", lat: 36.3489, lon: 43.1571 },
  { name: "السليمانية", lat: 35.5572, lon: 45.4356 },
  { name: "كركوك", lat: 35.4681, lon: 44.3922 },
  { name: "النجف", lat: 31.9996, lon: 44.3147 },
  { name: "كربلاء", lat: 32.6163, lon: 44.0249 },
  { name: "دبي", lat: 25.2048, lon: 55.2708 },
  { name: "الرياض", lat: 24.7136, lon: 46.6753 },
];

function detectCity(text: string): City {
  for (const city of CITIES) {
    if (text.includes(city.name)) return city;
  }
  return CITIES[0];
}

export async function fetchWeatherSnapshot(text: string): Promise<string | null> {
  if (!isWeatherQuestion(text)) return null;
  const city = detectCity(text);
  const url =
    `https://api.open-meteo.com/v1/forecast?latitude=${city.lat}&longitude=${city.lon}` +
    `&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m` +
    `&timezone=auto`;
  const res = await fetch(url);
  if (!res.ok) return null;
  const data = (await res.json()) as {
    current?: {
      temperature_2m?: number;
      relative_humidity_2m?: number;
      weather_code?: number;
      wind_speed_10m?: number;
    };
  };
  const cur = data.current;
  if (!cur || cur.temperature_2m == null) return null;
  return [
    `بيانات طقس حالية لمدينة ${city.name} (Open-Meteo):`,
    `• درجة الحرارة: ${cur.temperature_2m}°C`,
    `• الرطوبة: ${cur.relative_humidity_2m ?? "—"}%`,
    `• سرعة الرياح: ${cur.wind_speed_10m ?? "—"} كم/س`,
    `• رمز الطقس: ${cur.weather_code ?? "—"}`,
  ].join("\n");
}

function localFrishtaReply(last: string, weather?: string | null): string {
  const text = (last || "").trim();
  if (weather) {
    return `أنا Frishta AI.\n\n${weather}\n\nإذا تريد مدينة أخرى، اذكر اسمها.`;
  }
  if (!text) return "أنا Frishta AI. تفضل، ما الذي تريد الحديث عنه؟";
  const lower = text.toLowerCase();
  const greeting =
    /^(مرحبا|مرحباً|اهلا|أهلا|السلام عليكم|السلام|هلا|hi|hello|hey)([!.؟?\s،]|$)/i.test(text);
  if (greeting) return "أهلًا! أنا Frishta AI. كيف أقدر أساعدك؟";
  if (lower.includes("كيف حالك") || lower.includes("كيفك") || lower.includes("شلونك")) {
    return "بخير، شكرًا لسؤالك. أنا Frishta AI وجاهز أساعدك.";
  }
  if (
    lower.includes("من أنت") ||
    lower.includes("من انت") ||
    lower.includes("ما اسمك") ||
    lower.includes("اسمك")
  ) {
    return "أنا Frishta AI. هذا رد محلي من Frishta وليس ردًا من مزود خارجي.";
  }
  return (
    `أنا Frishta AI. وصلتني رسالتك: «${text.slice(0, 160)}».\n\n` +
    "هذا رد Frishta المحلي. لن أنسبه إلى ChatGPT أو Gemini أو DeepSeek أو Claude بدون مسار حقيقي موثّق."
  );
}

function externalNotConfigured(provider: string): ChatResult {
  const name = externalDisplayName(provider);
  return {
    answer:
      `${name} غير متصل بمسار حساب حقيقي معتمد داخل Frishta. ` +
      "واجهات API المدفوعة/حسب الاستهلاك معطلة بسياسة الكلفة الصفرية، ولا يوجد fallback محلي باسم هذا المزود.",
    provider,
    model: "unverified-account-runtime",
    status: "NOT_CONFIGURED",
  };
}

/**
 * Paid Gemini coder API is intentionally disabled. Candidate coding must use an explicitly
 * admitted zero-cost path; Codex remains manual-only and is not invoked here.
 */
export async function generateCoderText(_env: Env, _prompt: string): Promise<ChatResult> {
  return {
    answer:
      "Provider-backed code generation is NOT_CONFIGURED: paid/metered Gemini API execution is disabled by Frishta zero-cost policy.",
    provider: "gemini",
    model: "disabled-paid-api",
    status: "NOT_CONFIGURED",
  };
}

export async function resolveChat(
  _env: Env,
  providerRaw: string | undefined,
  messages: ChatMessage[],
): Promise<ChatResult> {
  const provider = (providerRaw || "auto").toLowerCase().trim() || "auto";
  const last = messages.at(-1)?.content ?? "";

  if (EXTERNAL_PROVIDERS.has(provider)) {
    return externalNotConfigured(provider);
  }

  if (provider === "auto" || provider === "frishta") {
    const weather = await fetchWeatherSnapshot(last);
    return {
      answer: localFrishtaReply(last, weather),
      provider: "frishta",
      model: weather ? "open-meteo+local-frishta" : "local-frishta",
      status: weather ? "OK" : "LOCAL_FALLBACK",
    };
  }

  return {
    answer: `المزود «${provider}» غير معتمد داخل Frishta. لم يتم تشغيل أي API أو fallback باسمه.`,
    provider,
    model: "unverified-provider",
    status: "NOT_CONFIGURED",
  };
}

/**
 * Configuration flags are policy flags, not secret-presence flags. Even if an old environment
 * still contains an API key, this worker refuses to use paid/metered LLM APIs.
 */
export function chatConfigFlags(_env: Env) {
  return {
    openai_configured: false,
    gemini_configured: false,
    deepseek_configured: false,
    paid_api_execution_allowed: false,
    account_runtime_verified: false,
  };
}
