from dotenv import load_dotenv
from datetime import datetime, timedelta ,date
import os
import sqlite3
import requests
import asyncio
import asyncio
import random
import requests
import json

ADMIN_ID = 7445334536
with open("merged_problems.json", "r", encoding="utf-8") as f:
    data = json.load(f)

PROBLEMS = data["questions"]

print("Loaded problems:", len(PROBLEMS))
 

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ---------------- ENV ----------------
load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")



# ---------------- DATABASE ----------------

import sqlite3
import json

conn = sqlite3.connect(
    "database.db",
    check_same_thread=False
)

cursor = conn.cursor()


# ---------------- USERS TABLE ----------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    telegram_id INTEGER UNIQUE NOT NULL,
    username TEXT,
    leetcode TEXT,
    joined_date TEXT,

    -- streak system
    streak INTEGER DEFAULT 1,
    longest_streak INTEGER DEFAULT 1,
    last_active TEXT,

    -- reminder system
    reminder_time TEXT,
    reminder_enabled INTEGER DEFAULT 0,

    -- problem tracking
    solved_problems TEXT DEFAULT '[]',
    solved_topics TEXT DEFAULT '[]',

    -- future daily POTD cache
    daily_potd TEXT,
    daily_potd_date TEXT,

    -- smart recommendation system
    last_recommended_problem TEXT,
    recommendation_date TEXT,

    -- gamification (future)
    xp INTEGER DEFAULT 0,
    level INTEGER DEFAULT 1
)
""")

conn.commit()


# ---------------- DATABASE MIGRATION ----------------
# Adds new columns to existing databases if they are missing

try:
    cursor.execute("""
        ALTER TABLE users
        ADD COLUMN joined_date TEXT
    """)
    conn.commit()

except sqlite3.OperationalError:
    pass


# ---------------- ANALYTICS TABLE ----------------

cursor.execute("""
CREATE TABLE IF NOT EXISTS analytics(
    feature TEXT PRIMARY KEY,
    count INTEGER DEFAULT 0
)
""")

conn.commit()


# ---------------- INDEXES ----------------

cursor.execute("""
CREATE INDEX IF NOT EXISTS idx_users_telegram
ON users(telegram_id)
""")

cursor.execute("""
CREATE INDEX IF NOT EXISTS idx_users_reminder
ON users(reminder_enabled)
""")

conn.commit()
# ---------------- STATE ----------------
ASK_LEETCODE = 1
ASK_BROADCAST = 2
CONFIRM_BROADCAST = 3

def get_leetcode_stats(username):
    url = "https://leetcode.com/graphql"

    query = """
    query getUserProfile($username: String!) {
      matchedUser(username: $username) {
        submitStats: submitStatsGlobal {
          acSubmissionNum {
            difficulty
            count
          }
        }
        profile {
          ranking
        }
      }
    }
    """

    variables = {"username": username}

    try:
        response = requests.post(
            url,
            json={
                "query": query,
                "variables": variables
            },
            timeout=10
        )

        print("Status:", response.status_code)
        print("Response:", response.text)

        data = response.json()
        user = data["data"]["matchedUser"]

        if not user:
            return None

        stats = user["submitStats"]["acSubmissionNum"]

        return {
            "total": stats[0]["count"],
            "easy": stats[1]["count"],
            "medium": stats[2]["count"],
            "hard": stats[3]["count"],
            "ranking": user["profile"]["ranking"],
        }

    except Exception as e:
        print("LeetCode API error:", e)
        return None


#---------------- ANALYTICS  ----------------
def increment_feature(feature):

    cursor.execute("""
        INSERT INTO analytics(feature, count)
        VALUES(?, 1)
        ON CONFLICT(feature)
        DO UPDATE SET count = count + 1
    """, (feature,))

    conn.commit()

#---------------- READ COUNTS ----------------
def get_feature_count(feature):

    row = cursor.execute("""
        SELECT count
        FROM analytics
        WHERE feature = ?
    """, (feature,)).fetchone()

    if row:
        return row[0]

    return 0
# ---------------- GET WEAK TOPICS STATS  ----------------  
def get_lc_topic_stats(username):
    print("Username received:", username)
    try:
        url = "https://leetcode.com/graphql"

        query = """
        query getUserProfile($username: String!) {
          matchedUser(username: $username) {
            tagProblemCounts {
              advanced {
                tagName
                problemsSolved
              }
              intermediate {
                tagName
                problemsSolved
              }
              fundamental {
                tagName
                problemsSolved
              }
            }
          }
        }
        """

        payload = {
            "query": query,
            "variables": {
                "username": username
            }
        }

        response = requests.post(
            url,
            json=payload,
            timeout=10
        )

        print("Status:", response.status_code)
        print("Response:", response.text)

        if response.status_code != 200:
            return None

        data = response.json()

        user = data["data"]["matchedUser"]

        if not user:
            return None

        topic_stats = {}

        for section in user["tagProblemCounts"].values():
            for topic in section:
                topic_stats[
                    topic["tagName"]
                ] = topic["problemsSolved"]

        return topic_stats

    except Exception as e:
        print("Topic stats error:", e)
        return None
# ---------------- POTD ----------------
def get_potd_level(user_id):
    day_seed = int(date.today().strftime("%Y%m%d")) + user_id
    random.seed(day_seed)

    levels = ["Easy", "Medium", "Hard"]
    return random.choice(levels)



def get_potd(user_id):
    try:
        level = get_potd_level(user_id)

        result = cursor.execute("""
            SELECT solved_problems
            FROM users
            WHERE telegram_id = ?
        """, (user_id,)).fetchone()

        if not result or not result[0]:
            solved = []
        else:
            try:
                solved = json.loads(result[0])
            except:
                solved = []

        # ---------------- NORMALIZE SOLVED ----------------
        solved_set = {
            str(x).strip().lower()
            for x in solved
            if x
        }

        # ---------------- FILTER UNSOLVED ----------------
        unsolved = []

        for p in PROBLEMS:
            slug = str(p.get("problem_slug", "")).strip().lower()

            if not slug:
                continue

            if slug not in solved_set:
                unsolved.append(p)

        # ---------------- SAFETY CHECK ----------------
        if not unsolved:
            return None, None

        # ---------------- OPTIONAL: LEVEL FILTER ----------------
        level_filtered = [
            p for p in unsolved
            if str(p.get("difficulty", "")).lower() == level.lower()
        ]

        final_list = level_filtered if level_filtered else unsolved

        return random.choice(final_list), level

    except Exception as e:
        print("get_potd error:", e)
        return None, None

# ---------------- START ----------------

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):
    user_id = update.effective_user.id
    username = (
        update.effective_user.username
        or update.effective_user.first_name
        or "Unknown User"
    )

    today = datetime.now().date()

    result = cursor.execute("""
        SELECT last_active, streak, longest_streak
        FROM users
        WHERE telegram_id = ?
    """, (user_id,)).fetchone()

    if result:

        last_active, streak, longest_streak = result

        if last_active:

            last_active = datetime.strptime(
                last_active,
                "%Y-%m-%d"
            ).date()

            if last_active == today:
                pass

            elif last_active == today - timedelta(days=1):

                streak += 1

                if streak > longest_streak:
                    longest_streak = streak

            else:
                streak = 1

        else:
            streak = 1

        cursor.execute("""
            UPDATE users
            SET username = ?,
                streak = ?,
                longest_streak = ?,
                last_active = ?
            WHERE telegram_id = ?
        """, (
            username,
            streak,
            longest_streak,
            today.isoformat(),
            user_id
        ))

    else:

        cursor.execute("""
            INSERT INTO users
            (
                telegram_id,
                username,
                leetcode,
                joined_date,
                streak,
                longest_streak,
                last_active
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (
            user_id,
            username,
            None,
            today.isoformat(),
            1,
            1,
            today.isoformat()
        ))

    conn.commit()

    # ---------------- MENU ----------------

    keyboard = [
        [InlineKeyboardButton("👤 Profile", callback_data="profile")],
        [InlineKeyboardButton("📈 LeetCode Stats", callback_data="leetcode")],
        [InlineKeyboardButton("🔥 Daily Challenge", callback_data="potd")],
        [InlineKeyboardButton("🧠 Smart Recommendation", callback_data="smart")],
        [InlineKeyboardButton("⏰ Reminders", callback_data="reminders")]
    ]

    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Welcome, {username}! 👋\n\nChoose an option:",
        reply_markup=reply_markup
    )

# ---------------- SET LEETCODE ----------------
async def set_leetcode(update: Update,
                       context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "Send your LeetCode username:"
    )

    return ASK_LEETCODE

# ---------------- SAVE LEETCODE ----------------
async def save_leetcode(update: Update,
                        context: ContextTypes.DEFAULT_TYPE):

    print("Entered save_leetcode()")

    user_id = update.effective_user.id
    lc_username = update.message.text

    print("Username received:", lc_username)

    cursor.execute("""
    UPDATE users
    SET leetcode = ?
    WHERE telegram_id = ?
    """, (lc_username, user_id))

    print("Rows updated:", cursor.rowcount)

    conn.commit()

    print("Committed to database")

    await update.message.reply_text(
        f"✅ Saved LeetCode username: {lc_username}"
    )

    print("Reply sent")

    return ConversationHandler.END
# ---------------- SET REMINDER ----------------
async def set_reminder(update: Update,
                       context: ContextTypes.DEFAULT_TYPE):

    if len(context.args) != 1:
        await update.message.reply_text(
            "Usage: /setreminder HH:MM\nExample: /setreminder 21:30"
        )
        return

    reminder_time = context.args[0]

    if len(reminder_time) != 5 or reminder_time[2] != ":":
        await update.message.reply_text(
            "Invalid time format.\nExample: /setreminder 21:30"
        )
        return

    user_id = update.effective_user.id

    cursor.execute("""
    UPDATE users
    SET reminder_time = ?,
        reminder_enabled = 1
    WHERE telegram_id = ?
    """, (reminder_time, user_id))

    conn.commit()

    await update.message.reply_text(
        f"⏰ Daily reminder set for {reminder_time}"
    )



# ---------------- UPDATE STREAK ----------------

from datetime import datetime, date

def update_streak(user_id):
    today = date.today()

    result = cursor.execute("""
    SELECT streak, longest_streak, last_active
    FROM users
    WHERE telegram_id = ?
    """, (user_id,)).fetchone()

    if not result:
        return

    streak, longest_streak, last_active = result

    if last_active:
        last_date = datetime.strptime(last_active, "%Y-%m-%d").date()
    else:
        last_date = None

    if not last_date:
        streak = 1

    elif last_date == today:
        return

    elif (today - last_date).days == 1:
        streak += 1

    else:
        streak = 1

    longest_streak = max(longest_streak or 0, streak)

    cursor.execute("""
    UPDATE users
    SET streak = ?,
        longest_streak = ?,
        last_active = ?
    WHERE telegram_id = ?
    """, (streak, longest_streak, today.isoformat(), user_id))

    conn.commit()

# ---------------- HELP ----------------
async def help_command(update: Update,
                       context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "/start - Start the bot\n"
        "/help - Show commands\n"
        "/about - About the bot\n"
        "/setleetcode - Set your LeetCode username\n"
        "/setreminder HH:MM - Set daily reminder"
    )

# ---------------- ABOUT ----------------
async def about(update: Update,
                context: ContextTypes.DEFAULT_TYPE):

    await update.message.reply_text(
        "🤖 Personal CP Assistant Bot\n"
        "Created by Achyut Mishra 🚀"
    )

#---------------- POTD ----------------


def get_potd_level(user_id):
    seed = int(date.today().strftime("%Y%m%d")) + user_id
    random.seed(seed)
    return random.choice(["Easy", "Medium", "Hard"])




def get_potd(user_id):
    try:
        level = get_potd_level(user_id)

        # ---------------- GET SOLVED ----------------
        result = cursor.execute("""
            SELECT solved_problems
            FROM users
            WHERE telegram_id = ?
        """, (user_id,)).fetchone()

        if not result or not result[0]:
            solved = []
        else:
            try:
                solved = json.loads(result[0])
            except:
                solved = []

        # ---------------- NORMALIZE SOLVED ----------------
        solved_set = {
            str(x).strip().lower()
            for x in solved
            if x
        }

        # ---------------- FILTER UNSOLVED ----------------
        unsolved = []

        for p in PROBLEMS:
            slug = str(p.get("problem_slug", "")).strip().lower()

            if not slug:
                continue

            if slug not in solved_set:
                unsolved.append(p)

        # ---------------- SAFETY CHECK ----------------
        if not unsolved:
            return None, None

        # ---------------- LEVEL FILTER ----------------
        level_filtered = [
            p for p in unsolved
            if str(p.get("difficulty", "")).lower() == level.lower()
        ]

        final_pool = level_filtered if level_filtered else unsolved

        # ---------------- RETURN RANDOM ----------------
        return random.choice(final_pool), level

    except Exception as e:
        print("get_potd error:", e)
        return None, None
        # ---------------- FILTER BY LEVEL + UNSOLVED ----------------
        candidates = [
            p for p in PROBLEMS
            if str(p.get("problem_slug", "")).strip().lower() not in solved_set
            and str(p.get("difficulty", "")).lower() == level.lower()
        ]

        # ---------------- FALLBACK (ANY UNSOLVED) ----------------
        if not candidates:
            candidates = [
                p for p in PROBLEMS
                if str(p.get("problem_slug", "")).strip().lower() not in solved_set
            ]

        # ---------------- FINAL SAFETY CHECK ----------------
        if not candidates:
            return None, level

        return random.choice(candidates), level

    except Exception as e:
        print("get_potd error:", e)
        return None, None

def mark_solved(user_id, problem_slug):
    result = cursor.execute("""
        SELECT solved_problems
        FROM users
        WHERE telegram_id = ?
    """, (user_id,)).fetchone()

    solved = json.loads(result[0] or "[]") if result else []

    if problem_slug not in solved:
        solved.append(problem_slug)

    cursor.execute("""
        UPDATE users
        SET solved_problems = ?
        WHERE telegram_id = ?
    """, (
        json.dumps(solved),
        user_id
    ))

    conn.commit()

# ---------------- WEAK TOPICS ----------------
from collections import Counter

from collections import Counter

def get_weak_topic(user_id):
    try:
        row = cursor.execute("""
            SELECT leetcode
            FROM users
            WHERE telegram_id = ?
        """, (user_id,)).fetchone()

        if not row or not row[0]:
            return None

        username = row[0]

        stats = get_lc_topic_stats(username)

        if not stats:
            return None

        stats = {
            topic: cnt
            for topic, cnt in stats.items()
            if cnt > 0
        }

        if not stats:
            return None

        minimum = min(stats.values())

        weak_topics = [
            topic
            for topic, cnt in stats.items()
            if cnt == minimum
        ]

        return random.choice(weak_topics)

    except Exception as e:
        print("Weak topic error:", e)
        return None

# ---------------- SMART RECOMMENDATION ----------------
def get_smart_recommendation(user_id):
    try:
        weak_topic = get_weak_topic(user_id)

        result = cursor.execute("""
            SELECT solved_problems
            FROM users
            WHERE telegram_id = ?
        """, (user_id,)).fetchone()

        solved = json.loads(result[0] or "[]") if result else []

        solved = set(
            str(x).strip().lower()
            for x in solved
        )

        # ---------------- Weak-topic recommendation ----------------
        if weak_topic:

            candidates = [
                p for p in PROBLEMS
                if weak_topic in p.get("topics", [])
                and str(
                    p.get("problem_slug", "")
                ).strip().lower() not in solved
            ]

            if candidates:
                return (
                    random.choice(candidates),
                    weak_topic
                )

        # ---------------- Fallback ----------------
        problem, _ = get_potd(user_id)
        return problem, None

    except Exception as e:
        print("Smart recommendation error:", e)
        return None, None

# ---------------- BUTTONS ----------------
import json
import random

async def button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        query = update.callback_query
        await query.answer()

        user_id = query.from_user.id

        # ---------------- PROFILE ----------------
        if query.data == "profile":
            increment_feature("profile")
            result = cursor.execute("""
                SELECT username, leetcode,
                       streak, longest_streak,
                       last_active
                FROM users
                WHERE telegram_id = ?
            """, (user_id,)).fetchone()

            if not result:
                await query.edit_message_text("No profile found. Send /start first.")
                return

            username, lc, streak, longest_streak, last_active = result

            await query.edit_message_text(
                f"👤 Profile\n\n"
                f"Name: {username}\n"
                f"LeetCode: {lc if lc else 'Not set'}\n"
                f"🔥 Current Streak: {streak} day(s)\n"
                f"🏆 Best Streak: {longest_streak} day(s)\n"
                f"📅 Last Active: {last_active}"
            )

        # ---------------- LEETCODE STATS ----------------
        elif query.data == "leetcode":
            increment_feature("leetcode")
            result = cursor.execute("""
                SELECT leetcode
                FROM users
                WHERE telegram_id = ?
            """, (user_id,)).fetchone()

            if not result or not result[0]:
                await query.edit_message_text(
                    "❌ Please set your LeetCode username using /setleetcode"
                )
                return

            username = result[0]
            update_streak(user_id)

            await context.bot.send_chat_action(
                chat_id=query.message.chat_id,
                action="typing"
            )

            await asyncio.sleep(1.5)

            await query.edit_message_text("⏳ Fetching LeetCode stats...")

            stats = get_leetcode_stats(username)

            if not stats:
                await query.edit_message_text("❌ Failed to fetch LeetCode stats")
                return

            await query.edit_message_text(
                f"📊 *LeetCode Dashboard*\n"
                f"━━━━━━━━━━━━━━\n\n"
                f"👤 *User:* `{username}`\n\n"
                f"🏆 *Total Solved:* `{stats['total']}`\n"
                f"🟢 *Easy:* `{stats['easy']}`\n"
                f"🟡 *Medium:* `{stats['medium']}`\n"
                f"🔴 *Hard:* `{stats['hard']}`\n\n"
                f"📈 *Ranking:* `{stats['ranking']}`\n\n"
                f"━━━━━━━━━━━━━━\n"
                f"💡 *Keep grinding. Consistency wins.*",
                parse_mode="Markdown"
            )

        
      # ---------------- PROBLEM OF THE DAY ----------------  
       
        elif query.data == "potd":
            increment_feature("potd")
            try:
                problem, level = get_potd(user_id)

                if not problem:
                    await query.edit_message_text(
                        "🔥 You have solved all available problems!"
                    )
                    return

                # ---------------- SAFE FIELD HANDLING ----------------
                title = problem.get("title", "Unknown")
                slug = problem.get("problem_slug", "")
                difficulty = problem.get("difficulty", "N/A")
                topics = problem.get("topics", [])

                # ---------------- SAFE TOPIC FORMAT ----------------
                if isinstance(topics, str):
                    topics = [topics]

                await query.edit_message_text(
                    f"🔥 Problem of the Day ({level})\n\n"
                    f"📌 {title}\n"
                    f"🎯 Difficulty: {difficulty}\n"
                    f"🏷 Topics: {', '.join(topics) if topics else 'N/A'}\n\n"
                    f"🔗 https://leetcode.com/problems/{slug}/\n\n"
                    f"👉 Solve it and then mark it as completed 👇",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton(
                                "✅ Mark as Solved",
                                callback_data=f"solved|{slug}"
                            )
                        ]
                    ])
                )

            except Exception as e:
                print("POTD ERROR:", e)
                try:
                    await query.edit_message_text(
                        "⚠️ Error fetching problem. Please try again later."
                    )
                except:
                    pass 

        # ---------------- MARK SOLVED ----------------
        elif query.data.startswith("solved|"):

            slug = query.data.split("|", 1)[1]

            mark_solved(user_id, slug)
            update_streak(user_id)

            await query.edit_message_text(
                f"🎉 Great job!\n\n"
                f"✔ Solved: {slug}\n"
                f"🔥 Streak updated!"
            )        
        # ---------------- SMART RECOMMENDATION ----------------
        elif query.data == "smart":
            increment_feature("smart")
            problem, topic = get_smart_recommendation(user_id)

            if not problem:
                await query.edit_message_text(
                    "🎉 You solved all available problems!"
                )
                return

            reason = ""

            if topic:
                reason = (
                    f"🧠 Based on your LeetCode profile,\n"
                    f"you need more practice in:\n"
                    f"🏷 {topic}\n\n"
                )

            await query.edit_message_text(
                reason +
                f"📌 {problem['title']}\n"
                f"🎯 Difficulty: {problem['difficulty']}\n"
                f"🏷 Topics: {', '.join(problem.get('topics', []))}\n\n"
                f"🔗 https://leetcode.com/problems/{problem['problem_slug']}/"
            )
        # ---------------- REMINDERS ----------------
        elif query.data == "reminders":
            increment_feature("reminders")
            result = cursor.execute("""
                SELECT reminder_enabled, reminder_time
                FROM users
                WHERE telegram_id = ?
            """, (user_id,)).fetchone()

            if not result:
                await query.edit_message_text(
                    "❌ No reminder settings found.\nUse /setreminder first."
                )
                return

            enabled, reminder_time = result
            status = "ON ✅" if enabled == 1 else "OFF ❌"

            await query.edit_message_text(
                f"⏰ *Reminder Settings*\n\n"
                f"Status: {status}\n"
                f"Time: {reminder_time if reminder_time else 'Not set'}\n\n"
                f"👉 You will receive daily POTD at this time.",
                parse_mode="Markdown"
            )

        # ---------------- DEFAULT ----------------
        else:
            await query.edit_message_text("❌ Unknown option")

    except Exception as e:
        print("BUTTON ERROR:", e)

        try:
            await query.edit_message_text(
                "⚠️ Something went wrong. Please try again."
            )
        except:
            pass




def get_potd(user_id):
    try:
        level = get_potd_level(user_id)

        row = cursor.execute("""
            SELECT solved_problems
            FROM users
            WHERE telegram_id = ?
        """, (user_id,)).fetchone()

        if not row:
            solved = []
        else:
            try:
                solved = json.loads(row[0]) if row[0] else []
            except:
                solved = []

        solved_set = {
            str(x).strip().lower()
            for x in solved
            if x
        }

        unsolved = []

        for p in PROBLEMS:
            slug = p.get("problem_slug")

            if not slug:
                continue

            if str(slug).strip().lower() not in solved_set:
                unsolved.append(p)

        if not unsolved:
            return None, None

        problem = random.choice(unsolved)

        return problem, level

    except Exception as e:
        print("get_potd error:", e)
        return None, None
        
#----------------REMINDER LOOP----------------

async def reminder_loop(app):
    while True:
        try:
            current_time = datetime.now().strftime("%H:%M")

            print("⏰ Scheduler running at:", datetime.now().strftime("%H:%M:%S"))

            users = cursor.execute("""
                SELECT telegram_id, reminder_time
                FROM users
                WHERE reminder_enabled = 1
            """).fetchall()

            for user_id, reminder_time in users:
                if not reminder_time:
                    continue

                reminder_time = reminder_time[:5]  # normalize HH:MM

                if reminder_time == current_time:
                    await app.bot.send_message(
                        chat_id=user_id,
                        text="⏰ Reminder: Time to solve your LeetCode problem!"
                    )

        except Exception as e:
            print("Scheduler error:", e)

        await asyncio.sleep(60)

# ---------------- APP ----------------

from datetime import datetime
import random


# ---------------- REMINDER JOB 
async def reminder_job(context: ContextTypes.DEFAULT_TYPE):
    application = context.application
    current_time = datetime.now().strftime("%H:%M")

    try:
        print("🔁 Scheduler running:", datetime.now().strftime("%H:%M:%S"))

        users = cursor.execute("""
            SELECT telegram_id, reminder_time
            FROM users
            WHERE reminder_enabled = 1
        """).fetchall()

        for user_id, reminder_time in users:

            if not reminder_time:
                continue

            reminder_time = reminder_time.strip()

            # ---------------- TIME MATCH ----------------
            if reminder_time[:5] != current_time:
                continue

            # ---------------- 1. ALWAYS SEND REMINDER ----------------
            await application.bot.send_message(
                chat_id=user_id,
                text="⏰ Reminder: Time to solve your LeetCode problem!"
            )

            # ---------------- 2. GET POTD ----------------
            problem, level = get_potd(user_id)

            # ---------------- 3. SEND POTD ONLY IF EXISTS ----------------
            if problem:
                await application.bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"🔥 Problem of the Day ({level})\n\n"
                        f"📌 {problem['title']}\n"
                        f"🎯 Difficulty: {problem.get('difficulty', 'N/A')}\n\n"
                        f"🔗 https://leetcode.com/problems/{problem['problem_slug']}/\n\n"
                        f"💡 Solve it and improve your streak!"
                    )
                )
            else:
                # optional fallback message (no unsolved problems)
                await application.bot.send_message(
                    chat_id=user_id,
                    text="🎉 Great job! You’ve solved all available problems!"
                )

    except Exception as e:
        print("Scheduler error:", e)


# ---------------- ADMIN ----------------
async def admin(update: Update,
                context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text("❌ Unauthorized.")
        return

    # ---------------- USER ANALYTICS ----------------

    total_users = cursor.execute("""
        SELECT COUNT(*)
        FROM users
    """).fetchone()[0]

    today = datetime.now().date().isoformat()

    joined_today = cursor.execute("""
        SELECT COUNT(*)
        FROM users
        WHERE joined_date = ?
    """, (today,)).fetchone()[0]

    active_today = cursor.execute("""
        SELECT COUNT(*)
        FROM users
        WHERE last_active = ?
    """, (today,)).fetchone()[0]

    top_streaks = cursor.execute("""
    SELECT username, longest_streak
    FROM users
    ORDER BY longest_streak DESC
    LIMIT 10
""").fetchall()

    leaderboard = ""

    for i, (username, streak) in enumerate(top_streaks, start=1):

        if not username:
            username = "Unknown"

        if i == 1:
            prefix = "🥇"
        elif i == 2:
            prefix = "🥈"
        elif i == 3:
            prefix = "🥉"
        else:
            prefix = f"{i}."

        leaderboard += (
            f"{prefix} {username} — 🔥 {streak}\n"
        )
    
    # ---------------- FEATURE ANALYTICS ----------------

    profile_count = get_feature_count("profile")
    leetcode_count = get_feature_count("leetcode")
    potd_count = get_feature_count("potd")
    smart_count = get_feature_count("smart")
    reminder_count = get_feature_count("reminders")

    enabled_reminders = cursor.execute("""
    SELECT COUNT(*)
    FROM users
    WHERE reminder_enabled = 1
    """).fetchone()[0]
    # ---------------- BOT HEALTH ----------------

    try:
        cursor.execute("SELECT 1")
        database_status = "🟢 Connected"
    except:
        database_status = "🔴 Disconnected"

    scheduler_status = "🟢 Running"

    bot_status = "🟢 Online"    
    # ---------------- DASHBOARD ----------------

    await update.message.reply_text(
    f"📊 Bot Analytics\n\n"

    f"👥 Total Users: {total_users}\n"
    f"🆕 Joined Today: {joined_today}\n"
    f"🔥 Active Today: {active_today}\n\n"

    f"━━━━━━━━━━━━━━━━━━\n\n"

    f"📈 Feature Usage\n\n"

    f"👤 Profile               : {profile_count}\n"
    f"📊 LeetCode Stats        : {leetcode_count}\n"
    f"🔥 Daily POTD            : {potd_count}\n"
    f"🧠 Smart Recommendation  : {smart_count}\n"
    f"⏰ Reminder Settings     : {reminder_count}\n\n"

    f"━━━━━━━━━━━━━━━━━━\n\n"

    f"🏆 Top 10 Longest Streaks\n\n"

    f"{leaderboard}"

    f"\n━━━━━━━━━━━━━━━━━━\n\n"

    f"⏰ Reminder Analytics\n\n"

    f"Enabled Reminders : {enabled_reminders}\n"

    f"\n━━━━━━━━━━━━━━━━━━\n\n"

    f"🤖 Bot Health\n\n"

    f"🗄 Database : {database_status}\n"
    f"⏰ Scheduler : {scheduler_status}\n"
    f"🤖 Bot : {bot_status}\n"
)

# ---------------- BROADCAST ----------------
async def broadcast(update: Update,
                    context: ContextTypes.DEFAULT_TYPE):

    if update.effective_user.id != ADMIN_ID:
        await update.message.reply_text(
            "❌ Unauthorized."
        )
        return ConversationHandler.END

    await update.message.reply_text(
        "📢 Send the message you want to broadcast."
    )

    return ASK_BROADCAST

# ---------------- SEND BROADCAST ----------------
async def send_broadcast(update: Update,
                         context: ContextTypes.DEFAULT_TYPE):

    message = update.message.text

    users = cursor.execute("""
        SELECT telegram_id
        FROM users
    """).fetchall()

    sent = 0
    failed = 0

    await update.message.reply_text(
        "📡 Broadcasting..."
    )

    for (telegram_id,) in users:

        try:
            await context.bot.send_message(
                chat_id=telegram_id,
                text=message
            )

            sent += 1

        except Exception:
            failed += 1

    await update.message.reply_text(
        f"✅ Broadcast Complete!\n\n"
        f"Delivered: {sent}\n"
        f"Failed: {failed}"
    )

    return ConversationHandler.END
# ---------------- BUILD APP ----------------

app = Application.builder().token(TOKEN).build()

# ---------------- START JOB SCHEDULER ----------------

app.job_queue.run_repeating(
    reminder_job,
    interval=60,
    first=5
)

# ---------------- COMMAND HANDLERS ----------------

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("help", help_command))
app.add_handler(CommandHandler("about", about))
app.add_handler(CommandHandler("setreminder", set_reminder))
app.add_handler(CommandHandler("admin", admin))
# ---------------- CONVERSATION HANDLER ----------------

conv_handler = ConversationHandler(
    entry_points=[CommandHandler("setleetcode", set_leetcode)],
    states={
        ASK_LEETCODE: [
            MessageHandler(filters.TEXT & ~filters.COMMAND, save_leetcode)
        ],
    },
    fallbacks=[]
)

app.add_handler(conv_handler)

# ---------------- BROADCAST HANDLER ----------------

broadcast_handler = ConversationHandler(
    entry_points=[
        CommandHandler("broadcast", broadcast)
    ],
    states={
        ASK_BROADCAST: [
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                send_broadcast
            )
        ]
    },
    fallbacks=[]
)

app.add_handler(broadcast_handler)
# ---------------- CALLBACK HANDLER ----------------

app.add_handler(CallbackQueryHandler(button))

# ---------------- START BOT ----------------

print("Bot started...")
print(get_lc_topic_stats("Achyut_Mishra_"))

print(get_weak_topic(7445334536))
for p in PROBLEMS[:5]:
    print(p["topics"])

increment_feature("test")

print(get_feature_count("test"))
print("Profile:", get_feature_count("profile"))
print("LeetCode:", get_feature_count("leetcode"))
print("POTD:", get_feature_count("potd"))
print("Smart:", get_feature_count("smart"))
print("Reminder:", get_feature_count("reminders"))
app.run_polling()