import type { Env } from "./types";

export type ChatMessage = { role: string; content: string };

export type ChatResult = {
  answer: string;
  provider: string;
  model: string;
  status: "OK" | "LOCAL_FALLBACK" | "NOT_CONFIGURED" | "ERROR";
};

const PROVIDER_NAMES: Record<string, string> = {
  auto: "Frishta AI",
  frishta: "Frishta AI",
  chatgpt: "ChatGPT",
  gemini: "Gemini",
  claude: "Claude",
  deepseek: "DeepSeek",
};

function providerDisplayName(provider: string): string {
  return PROVIDER_NAMES[provider] ?? provider;
}

function systemPrompt(provider: string): string {
  const name = providerDisplayName(provider);
  return [
    `أنت ${name} داخل تطبيق Android اسمه Frishta AI (Candidate).`,
    `إذا سُئلت عن اسمك أو من أنت، قل صراحة: أنا ${name} داخل تطبيق Frishta AI.`,
    "أجب بالعربية الفصحى الواضحة إلا إذا كتب المستخدم بغير العربية.",
    "كن مفيدًا ومباشرًا وقصيرًا نسبيًا. لا تسأل أسئلة عامة غير ضرورية عن HTML/React — أنت داخل تطبيق Android جاهز.",
    "",
    "إمكانيات تطبيق Frishta AI التي يجب أن تعرفها وتقترحها عند الحاجة:",
    "1) محادثة ذكية داخل التطبيق (أنت المزود المختار الآن).",
    "2) الطقس ودرجة الحرارة عبر السحابة (Open-Meteo) عند السؤال عن الطقس.",
    "3) اختيار المزود من الشريط العلوي: Gemini / ChatGPT / DeepSeek / Claude / Auto.",
    "4) التعديل الذاتي للتطبيق: إذا طلب المستخدم تحسين الواجهة أو إصلاح خطأ في التطبيق، دله أن يكتب طلبًا واضحًا مثل «حسّن التطبيق: …» أو «عدّل جزء من …»، ثم بعد ظهور الخطة يقول «ابدأ». المسار: نسخة احتياطية → بناء سحابي → تثبيت → ويمكنه «ارجع للنسخة السابقة» عند الفشل.",
    "5) تثبيت APK جاهز: أرفق APK وقل «ثبت التحديث».",
    "6) النسخ الاحتياطي والرجوع من الإعدادات أو بعبارة «ارجع للنسخة السابقة».",
    "7) مشاريع ومهام سحابية عبر Hassan Cloud (بدون بطاقة ائتمان في سياسة الكلفة الصفرية).",
    "8) الميكروفون للكلام والرد الصوتي.",
    "",
    "قواعد صدق مهمة:",
    "- لا تدّعِ أنك مزود آخر غير المحدد.",
    "- لا تدّعِ أنك تعدّل ملفات الجهاز مباشرة بدون مسار التعديل الذاتي أعلاه.",
    "- ممنوع تمامًا اختراع اسم ملف APK أو القول إن التحديث جاهز أو أن البناء اكتمل، إلا إذا ظهرت رسالة نظام من Frishta تؤكد ذلك صراحة (نسخ احتياطي/مهمة سحابة/APK جاهز).",
    "- إذا طلب المستخدم «ثبت التحديث» بدون APK مرفق وبدون رسالة نظام سابقة عن جاهزية ملف حقيقي، قل له بصراحة أنه لا يوجد ملف تثبيت جاهز، واطلب إعادة «حسّن التطبيق: …» ثم «ابدأ».",
    "- إذا كان المزود بلا مفتاح API حقيقي، كن صادقًا عند السؤال عن ذلك.",
    "- عند طلب تحسين التطبيق: لا تقل إنك لا تستطيع؛ اشرح مسار Frishta للتعديل الذاتي واطلب تأكيد «ابدأ».",
    "- للأسئلة الواقعية مثل الطقس استخدم البيانات المعطاة لك إن وُجدت.",
  ].join("\n");
}

function normalizeRole(role: string): "user" | "assistant" | "system" {
  const r = role.toLowerCase();
  if (r === "assistant" || r === "hassan" || r === "provider" || r === "model") return "assistant";
  if (r === "system") return "system";
  return "user";
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
  // Default: Baghdad (user locale)
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

function localPersonaReply(provider: string, last: string, weather?: string | null): string {
  const name = providerDisplayName(provider);
  const text = (last || "").trim();
  const lower = text.toLowerCase();
  if (!text) return `أنا ${name}. تفضل، ما الذي تريد الحديث عنه؟`;

  if (weather) {
    return `أنا ${name}.\n\n${weather}\n\nإذا تريد مدينة أخرى، اذكر اسمها.`;
  }

  const greeting =
    /^(مرحبا|مرحباً|اهلا|أهلا|السلام عليكم|السلام|هلا|hi|hello|hey)([!.؟?\s،]|$)/i.test(text);
  if (greeting) return `أهلًا! أنا ${name}. كيف أقدر أساعدك؟`;

  if (lower.includes("كيف حالك") || lower.includes("كيفك") || lower.includes("شلونك")) {
    return `بخير، شكرًا لسؤالك. أنا ${name} وجاهز أساعدك.`;
  }

  if (
    lower.includes("من أنت") ||
    lower.includes("من انت") ||
    lower.includes("ما اسمك") ||
    lower.includes("اسمك") ||
    text.includes("من أنت")
  ) {
    return `أنا ${name}.`;
  }

  return (
    `أنا ${name}.\n` +
    `وصلتني رسالتك: «${text.slice(0, 160)}».\n\n` +
    `حاليًا مفتاح ${name} غير مفعّل على السحابة، لذلك أرد محليًا بهوية ${name}. ` +
    `فعّل مفتاح المزود على Hassan Cloud لردود ${name} الحقيقية.`
  );
}

async function callOpenAI(env: Env, provider: string, messages: ChatMessage[]): Promise<ChatResult> {
  const key = env.OPENAI_API_KEY?.trim();
  const last = messages.at(-1)?.content ?? "";
  if (!key) {
    const weather = await fetchWeatherSnapshot(last);
    return {
      answer: localPersonaReply(provider, last, weather),
      provider,
      model: "local-persona",
      status: weather ? "OK" : "NOT_CONFIGURED",
    };
  }
  const model = env.OPENAI_MODEL?.trim() || "gpt-4o-mini";
  const weather = await fetchWeatherSnapshot(messages.at(-1)?.content ?? "");
  const payloadMessages = [
    { role: "system", content: systemPrompt(provider) + (weather ? `\n\n${weather}` : "") },
    ...messages.map((m) => ({ role: normalizeRole(m.role), content: m.content })),
  ];
  const res = await fetch("https://api.openai.com/v1/chat/completions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ model, messages: payloadMessages }),
  });
  const raw = await res.text();
  if (!res.ok) {
    return {
      answer: `تعذر الاتصال بـ ChatGPT/OpenAI (${res.status}): ${raw.slice(0, 180)}`,
      provider: "chatgpt",
      model,
      status: "ERROR",
    };
  }
  const data = JSON.parse(raw) as {
    choices?: Array<{ message?: { content?: string } }>;
    model?: string;
  };
  const answer = data.choices?.[0]?.message?.content?.trim() || "";
  if (!answer) {
    return { answer: "رد فارغ من OpenAI.", provider: "chatgpt", model, status: "ERROR" };
  }
  return {
    answer,
    provider: provider === "auto" ? "chatgpt" : provider,
    model: data.model || model,
    status: "OK",
  };
}

async function callGemini(env: Env, messages: ChatMessage[]): Promise<ChatResult> {
  const key = env.GEMINI_API_KEY?.trim();
  if (!key) {
    const last = messages.at(-1)?.content ?? "";
    const weather = await fetchWeatherSnapshot(last);
    return {
      answer: localPersonaReply("gemini", last, weather),
      provider: "gemini",
      model: "local-persona",
      status: weather ? "OK" : "NOT_CONFIGURED",
    };
  }
  const model = env.GEMINI_MODEL?.trim() || "gemini-3.5-flash-lite";
  const weather = await fetchWeatherSnapshot(messages.at(-1)?.content ?? "");
  const contents = messages
    .filter((m) => normalizeRole(m.role) !== "system")
    .map((m) => ({
      role: normalizeRole(m.role) === "assistant" ? "model" : "user",
      parts: [{ text: m.content }],
    }));
  const url =
    `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${key}`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      systemInstruction: {
        parts: [{ text: systemPrompt("gemini") + (weather ? `\n\n${weather}` : "") }],
      },
      contents,
    }),
  });
  const raw = await res.text();
  if (!res.ok) {
    return {
      answer: `تعذر الاتصال بـ Gemini (${res.status}): ${raw.slice(0, 180)}`,
      provider: "gemini",
      model,
      status: "ERROR",
    };
  }
  const data = JSON.parse(raw) as {
    candidates?: Array<{ content?: { parts?: Array<{ text?: string }> } }>;
  };
  const answer =
    data.candidates?.[0]?.content?.parts?.map((p) => p.text || "").join("").trim() || "";
  if (!answer) {
    return { answer: "رد فارغ من Gemini.", provider: "gemini", model, status: "ERROR" };
  }
  return { answer, provider: "gemini", model, status: "OK" };
}

/** Raw Gemini completion for self-improve coder (no chat persona). */
export async function generateCoderText(env: Env, prompt: string): Promise<ChatResult> {
  const key = env.GEMINI_API_KEY?.trim();
  const model = env.GEMINI_MODEL?.trim() || "gemini-3.5-flash-lite";
  if (!key) {
    return {
      answer: "GEMINI_API_KEY not configured on Hassan Cloud worker",
      provider: "gemini",
      model,
      status: "NOT_CONFIGURED",
    };
  }
  const url =
    `https://generativelanguage.googleapis.com/v1beta/models/${model}:generateContent?key=${key}`;
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      systemInstruction: {
        parts: [
          {
            text:
              "You are a coding agent. Follow the user instructions exactly. " +
              "When asked for JSON, return ONLY valid JSON with no markdown fences.",
          },
        ],
      },
      contents: [{ role: "user", parts: [{ text: prompt }] }],
      generationConfig: { temperature: 0.2, maxOutputTokens: 8192 },
    }),
  });
  const raw = await res.text();
  if (!res.ok) {
    return {
      answer: `Gemini coder HTTP ${res.status}: ${raw.slice(0, 240)}`,
      provider: "gemini",
      model,
      status: "ERROR",
    };
  }
  const data = JSON.parse(raw) as {
    candidates?: Array<{ content?: { parts?: Array<{ text?: string }> } }>;
  };
  const answer =
    data.candidates?.[0]?.content?.parts?.map((p) => p.text || "").join("").trim() || "";
  if (!answer) {
    return { answer: "Empty Gemini coder response", provider: "gemini", model, status: "ERROR" };
  }
  return { answer, provider: "gemini", model, status: "OK" };
}

async function callDeepSeek(env: Env, messages: ChatMessage[]): Promise<ChatResult> {
  const key = env.DEEPSEEK_API_KEY?.trim();
  if (!key) {
    const last = messages.at(-1)?.content ?? "";
    const weather = await fetchWeatherSnapshot(last);
    return {
      answer: localPersonaReply("deepseek", last, weather),
      provider: "deepseek",
      model: "local-persona",
      status: "NOT_CONFIGURED",
    };
  }
  const model = "deepseek-chat";
  const weather = await fetchWeatherSnapshot(messages.at(-1)?.content ?? "");
  const res = await fetch("https://api.deepseek.com/chat/completions", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${key}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      model,
      messages: [
        { role: "system", content: systemPrompt("deepseek") + (weather ? `\n\n${weather}` : "") },
        ...messages.map((m) => ({ role: normalizeRole(m.role), content: m.content })),
      ],
    }),
  });
  const raw = await res.text();
  if (!res.ok) {
    return {
      answer: `تعذر الاتصال بـ DeepSeek (${res.status}): ${raw.slice(0, 180)}`,
      provider: "deepseek",
      model,
      status: "ERROR",
    };
  }
  const data = JSON.parse(raw) as {
    choices?: Array<{ message?: { content?: string } }>;
    model?: string;
  };
  const answer = data.choices?.[0]?.message?.content?.trim() || "";
  return {
    answer: answer || "رد فارغ من DeepSeek.",
    provider: "deepseek",
    model: data.model || model,
    status: answer ? "OK" : "ERROR",
  };
}

export async function resolveChat(
  env: Env,
  providerRaw: string | undefined,
  messages: ChatMessage[],
): Promise<ChatResult> {
  const provider = (providerRaw || "auto").toLowerCase().trim() || "auto";
  const last = messages.at(-1)?.content ?? "";

  // Weather can be answered even without LLM keys.
  if (provider === "auto" || provider === "frishta") {
    if (env.OPENAI_API_KEY?.trim()) return callOpenAI(env, "auto", messages);
    if (env.GEMINI_API_KEY?.trim()) return callGemini(env, messages);
    const weather = await fetchWeatherSnapshot(last);
    return {
      answer: localPersonaReply("frishta", last, weather),
      provider: "frishta",
      model: "local-persona",
      status: weather ? "OK" : "LOCAL_FALLBACK",
    };
  }

  if (provider === "chatgpt") return callOpenAI(env, "chatgpt", messages);
  if (provider === "gemini") return callGemini(env, messages);
  if (provider === "deepseek") return callDeepSeek(env, messages);
  if (provider === "claude") {
    const weather = await fetchWeatherSnapshot(last);
    return {
      answer: localPersonaReply("claude", last, weather),
      provider: "claude",
      model: "local-persona",
      status: "NOT_CONFIGURED",
    };
  }

  const weather = await fetchWeatherSnapshot(last);
  return {
    answer: localPersonaReply(provider, last, weather),
    provider,
    model: "local-persona",
    status: "LOCAL_FALLBACK",
  };
}

export function chatConfigFlags(env: Env) {
  return {
    openai_configured: !!(env.OPENAI_API_KEY && env.OPENAI_API_KEY.trim()),
    gemini_configured: !!(env.GEMINI_API_KEY && env.GEMINI_API_KEY.trim()),
    deepseek_configured: !!(env.DEEPSEEK_API_KEY && env.DEEPSEEK_API_KEY.trim()),
  };
}
