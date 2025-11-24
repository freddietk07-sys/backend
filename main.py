from fastapi import FastAPI
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from openai import OpenAI
import os

# Load .env variables
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

print("URL:", SUPABASE_URL)
print("KEY:", SUPABASE_SERVICE_KEY)

# Supabase client
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# OpenAI client
client = OpenAI(api_key=OPENAI_API_KEY)

# Test OpenAI connection
print("Testing OpenAI connection...")
try:
    test = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "Hello"}]
    )
    print("OpenAI test worked:", test.choices[0].message.content)
except Exception as e:
    print("OPENAI STARTUP ERROR:", e)

app = FastAPI()

class EmailPayload(BaseModel):
    inbox_id: str
    sender: str
    subject: str
    body: str


@app.post("/webhook/email")
async def process_email(payload: EmailPayload):

    system_prompt = (
        "You are an assistant that writes clear, polite, professional email replies. "
        "Keep replies concise, friendly, and helpful. "
        "If you are missing key information, ask for clarification."
    )

    user_prompt = f"""
    You are replying on behalf of a business.

    Incoming email:
    From: {payload.sender}
    Subject: {payload.subject}
    Body:
    {payload.body}

    Write a full reply email in a professional tone. 
    Do NOT include 'Subject:' in the reply — only return the email body.
    """

    try:
        completion = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.5,
        )

        ai_reply = completion.choices[0].message.content
        status = "draft"

    except Exception as e:
        print("OPENAI ERROR:", e)
        ai_reply = (
            "Hi, thanks for your email. Our automated system was unable to generate a reply, "
            "so a member of our team will follow up with you shortly."
        )
        status = "draft"

    supabase.table("email_logs").insert({
        "inbox_id": payload.inbox_id,
        "sender": payload.sender,
        "subject": payload.subject,
        "body": payload.body,
        "ai_reply": ai_reply,
        "confidence": 0.8,
        "status": status
    }).execute()

    return {"status": status, "reply": ai_reply}


