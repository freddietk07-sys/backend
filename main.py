from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from openai import OpenAI
import urllib.parse
import os
import requests
import time
import base64

# Load environment variables
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

print("Supabase URL:", SUPABASE_URL)
print("Supabase Service Key loaded:", SUPABASE_SERVICE_KEY is not None)
print("OpenAI Key loaded:", OPENAI_API_KEY is not None)

# Initialize Supabase
supabase: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)

# Initialize OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

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


# -------------------- DATA MODELS --------------------
class EmailPayload(BaseModel):
    inbox_id: str
    sender: str
    subject: str
    body: str


class SendEmailRequest(BaseModel):
    user_email: str
    to: str
    subject: str
    message: str


# -------------------- STEP 1: Generate Gmail OAuth URL --------------------
@app.get("/connect/gmail/{client_id}")
def connect_gmail(client_id: str):

    if not GOOGLE_CLIENT_ID or not GOOGLE_REDIRECT_URI:
        raise HTTPException(
            status_code=500,
            detail="Google OAuth environment variables missing"
        )

    oauth_params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "access_type": "offline",
        "prompt": "consent",
        "scope": "https://www.googleapis.com/auth/gmail.send"
    }

    base_url = "https://accounts.google.com/o/oauth2/v2/auth"
    oauth_url = f"{base_url}?{urllib.parse.urlencode(oauth_params)}"

    return {"oauth_url": oauth_url}


# -------------------- STEP 2: OAuth Callback (Token Exchange + Save) --------------------
@app.get("/oauth/gmail/callback")
async def gmail_callback(code: str):

    token_url = "https://oauth2.googleapis.com/token"

    data = {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": GOOGLE_REDIRECT_URI,
    }

    response = requests.post(token_url, data=data)
    tokens = response.json()
    print("TOKEN RESPONSE:", tokens)

    if "access_token" not in tokens:
        raise HTTPException(status_code=400, detail=tokens)

    # Extract values
    access_token = tokens["access_token"]
    refresh_token = tokens["refresh_token"]
    token_type = tokens["token_type"]
    scope = tokens["scope"]
    expires_in = tokens["expires_in"]
    expires_at = int(time.time()) + int(expires_in)

    # TEMPORARY — until you add real users
    user_email = "prod.tkmusic@gmail.com"

    supabase.table("gmail_tokens").insert({
        "user_email": user_email,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": token_type,
        "scope": scope,
        "expires_at": expires_at
    }).execute()

    return {"status": "saved", "email": user_email}


# -------------------- STEP 3: Token Refreshing --------------------
def refresh_gmail_token(user_email: str):

    result = supabase.table("gmail_tokens").select("*").eq("user_email", user_email).order("created_at", desc=True).limit(1).execute()

    if not result.data:
        raise HTTPException(status_code=404, detail="User has no stored Gmail tokens")

    record = result.data[0]

    # If token is still valid, return
    if record["expires_at"] > time.time():
        return record["access_token"]

    print("Access token expired — refreshing...")

    token_url = "https://oauth2.googleapis.com/token"
    data = {
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "refresh_token": record["refresh_token"],
        "grant_type": "refresh_token",
    }

    response = requests.post(token_url, data=data)
    new_tokens = response.json()
    print("REFRESH RESPONSE:", new_tokens)

    if "access_token" not in new_tokens:
        raise HTTPException(status_code=400, detail=new_tokens)

    new_access = new_tokens["access_token"]
    expires_at = int(time.time()) + int(new_tokens["expires_in"])

    # Save new token
    supabase.table("gmail_tokens").insert({
        "user_email": user_email,
        "access_token": new_access,
        "refresh_token": record["refresh_token"],
        "token_type": record["token_type"],
        "scope": record["scope"],
        "expires_at": expires_at
    }).execute()

    return new_access


# -------------------- STEP 4: Send Gmail Email --------------------
def send_gmail_message(user_email: str, to_addr: str, subject: str, message_body: str):

    access_token = refresh_gmail_token(user_email)

    gmail_url = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
    raw_email = f"To: {to_addr}\r\nSubject: {subject}\r\n\r\n{message_body}"

    encoded_message = base64.urlsafe_b64encode(raw_email.encode("utf-8")).decode("utf-8")

    payload = {"raw": encoded_message}

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }

    response = requests.post(gmail_url, json=payload, headers=headers)
    print("SEND RESPONSE:", response.json())

    if response.status_code != 200:
        raise HTTPException(status_code=500, detail=response.json())

    return response.json()


# -------------------- STEP 5: API Endpoint to Send Email --------------------
@app.post("/gmail/send")
def gmail_send(request: SendEmailRequest):
    result = send_gmail_message(
        user_email=request.user_email,
        to_addr=request.to,
        subject=request.subject,
        message_body=request.message
    )
    return {"status": "sent", "response": result}


# -------------------- EXISTING: EMAIL WEBHOOK --------------------
@app.post("/webhook/email")
async def process_email(payload: EmailPayload):

    system_prompt = (
        "You are an assistant that writes clear, polite, professional email replies. "
        "Keep replies concise, friendly, and helpful. "
        "If you are missing key information, ask for clarification."
    )

    user_prompt = f"""
    Write a professional reply email.

    Incoming email:
    From: {payload.sender}
    Subject: {payload.subject}
    Body:
    {payload.body}

    Reply in a helpful, polite way.
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
        ai_reply = "There was an error generating a reply."
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


# -------------------- SERVER ENTRYPOINT --------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
