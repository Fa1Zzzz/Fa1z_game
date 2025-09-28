import os, random, asyncio
import socketio
from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from starlette.staticfiles import StaticFiles
from uvicorn import run

# إعداد Socket.IO (ASGI)
sio = socketio.AsyncServer(async_mode="asgi", cors_allowed_origins="*")
app = FastAPI()
# خدمة الملفات الثابتة: /static  (HTML + الأصوات)
app.mount("/static", StaticFiles(directory="static"), name="static")
asgi_app = socketio.ASGIApp(sio, other_asgi_app=app)

# توجيه الجذر "/" لصفحة الواجهة
@app.get("/")
async def root():
    return RedirectResponse(url="/static/index.html")

# ====== إعدادات اللعبة ======
QUESTION_TIME = 12  # مدة السؤال بالثواني
PAUSE_BETWEEN = 3   # استراحة بين الأسئلة بالثواني

# بنك أسئلة (مع خيار "explain" اختياري يظهر تحت السؤال بعد reveal)
QUESTIONS = [
    {
        "q": "ما هي عاصمة اليابان؟",
        "choices": ["A) أوساكا", "B) طوكيو", "C) كيوتو", "D) كوبي"],
        "answer": "B",
        "explain": "طوكيو العاصمة منذ 1869 (بعد انتقالها من كيوتو)."
    },
    {
        "q": "أكبر كوكب في المجموعة الشمسية:",
        "choices": ["A) الأرض", "B) زحل", "C) المشتري", "D) أورانوس"],
        "answer": "C",
        "explain": "المشتري الأكبر حجمًا وكتلةً بين الكواكب."
    },
    {
        "q": "لغة البايثون ظهرت تقريبًا في:",
        "choices": ["A) الثمانينات", "B) التسعينات", "C) الألفينات", "D) 2010s"],
        "answer": "B",
        "explain": "تم إصدارها لأول مرة بداية التسعينات."
    },
]

# حالة السيرفر
scores = {}            # name -> score
names  = {}            # sid -> name
current_answer = None  # الحرف الصحيح للسؤال الجاري
running = False        # هل اللعبة دائرة الآن؟
answered_flag = False  # تم احتساب أول إجابة صحيحة لهذه الجولة؟

# ====== أحداث Socket.IO ======

@sio.event
async def connect(sid, environ):
    await sio.emit("system", {"msg": "✅ تم الاتصال بالسيرفر."}, to=sid)

@sio.event
async def join(sid, data):
    """انضمام لاعب جديد بالاسم"""
    name = (data.get("name") or "Player").strip()[:16]
    # اجعل الاسم فريدًا
    base = name
    i = 1
    while name in names.values():
        i += 1
        name = f"{base}{i}"
    names[sid] = name
    scores.setdefault(name, 0)
    await sio.emit("system", {"msg": f"{name} انضم للعبة. لاعبين: {len(names)}"})
    await sio.emit("joined", {"name": name}, to=sid)

@sio.event
async def answer(sid, data):
    """استلام إجابة لاعب"""
    global current_answer, answered_flag
    letter = (data.get("letter") or "").upper().strip()
    if not current_answer or letter not in {"A","B","C","D"}:
        return
    # أول إجابة صحيحة فقط
    if (not answered_flag) and (letter == current_answer):
        player = names.get(sid, "Player")
        scores[player] = scores.get(player, 0) + 1
        answered_flag = True
        await sio.emit("correct_first", {"player": player, "gain": 1})
        await broadcast_leaderboard()

@sio.event
async def start(sid):
    """بدء جولة أسئلة"""
    await quiz_loop()

@sio.event
async def disconnect(sid):
    name = names.pop(sid, None)
    if name:
        await sio.emit("system", {"msg": f"{name} غادر. لاعبين: {len(names)}"})

# ====== حلقات و مساعدات ======

async def broadcast_leaderboard():
    lb = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    await sio.emit("leaderboard", {"scores": lb})

async def quiz_loop():
    """حلقة الأسئلة الرئيسية"""
    global current_answer, running, answered_flag
    if running:
        return
    if not names:
        await sio.emit("system", {"msg": "لا يوجد لاعبين بعد. اطلب من اللاعبين الانضمام ثم اضغط بدء الجولة."})
        return

    running = True
    await sio.emit("system", {"msg": "🚀 بدء الجولة!"})

    qs = QUESTIONS[:]
    random.shuffle(qs)
    total = len(qs)

    for idx, item in enumerate(qs, start=1):
        # إعداد سؤال جديد
        current_answer = item["answer"].upper()
        answered_flag = False

        # إرسال السؤال
        await sio.emit("question", {
            "index": idx, "total": total,
            "q": item["q"], "choices": item["choices"], "time": QUESTION_TIME
        })

        # مؤقّت السؤال
        await asyncio.sleep(QUESTION_TIME)

        # إعلان الإجابة الصحيحة + الشرح (لو موجود)
        reveal_payload = {"answer": current_answer}
        if item.get("explain"):
            reveal_payload["explain"] = item["explain"]
        await sio.emit("reveal", reveal_payload)

        # بث لوحة الصدارة
        await broadcast_leaderboard()

        # استراحة قصيرة بين الأسئلة
        current_answer = None
        await asyncio.sleep(PAUSE_BETWEEN)

    # نهاية الجولة
    lb = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    top = lb[0][1] if lb else 0
    winners = [n for n, sc in lb if sc == top] if lb else []
    await sio.emit("game_over", {"winners": winners, "top_score": top, "scores": lb})
    running = False

# ====== تشغيل ======
if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8000"))
    run(asgi_app, host="0.0.0.0", port=port)
