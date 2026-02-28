import os
import json
import base64
import asyncio
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from google import genai
from google.genai import types
from dotenv import load_dotenv

# --- 1. Production Setup & Configuration ---
load_dotenv()  

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
TRANSFER_NUMBER = os.getenv("TRANSFER_NUMBER", "+1234567890")

if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, GEMINI_API_KEY]):
    logger.critical("Missing critical environment variables. Check your .env file.")
    exit(1)

twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
genai_client = genai.Client(api_key=GEMINI_API_KEY)

app = FastAPI(title="Twilio-Gemini Voice Assistant")

# --- 2. The TwiML Webhook ---
@app.post("/voice")
async def handle_voice(request: Request):
    host = request.url.hostname
    protocol = "wss" if request.url.scheme == "https" else "ws"
    logger.info(f"Incoming call received. Routing to {protocol}://{host}/media-stream")
    
    twiml = f"""
    <Response>
        <Say>Hello, I am your AI support assistant. How can I help you today?</Say>
        <Connect>
            <Stream url="{protocol}://{host}/media-stream" />
        </Connect>
    </Response>
    """
    return HTMLResponse(content=twiml, media_type="application/xml")

# --- 3. The Human Handoff Logic ---
def initiate_transfer(call_sid: str, reason: str):
    logger.info(f"Initiating transfer for Call SID: {call_sid}. Reason: {reason}")
    try:
        twiml_instruction = f"""
        <Response>
            <Say>I understand. Please hold while I connect you to a human agent.</Say>
            <Dial>{TRANSFER_NUMBER}</Dial>
        </Response>
        """
        twilio_client.calls(call_sid).update(twiml=twiml_instruction)
        logger.info(f"Successfully updated call {call_sid} for transfer.")
    except TwilioRestException as e:
        logger.error(f"Twilio API error during transfer: {e}")
    except Exception as e:
        logger.error(f"Unexpected error during transfer: {e}")

# --- 4. The WebSocket Bridge ---
@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket connection established with Twilio.")
    
    config = types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        system_instruction=(
            "You are a helpful customer support AI. Keep your answers brief and conversational. "
            "If the user asks to speak to a human, a manager, or seems highly frustrated, "
            "you MUST immediately use the 'transfer_to_human' tool."
        ),
        tools=[{
            "function_declarations": [{
                "name": "transfer_to_human",
                "description": "Connect the user to a live human agent.",
                "parameters": {
                    "type": "object",
                    "properties": {"reason": {"type": "string", "description": "Why the user wants a human"}},
                    "required": ["reason"]
                }
            }]
        }]
    )

    try:
        async with genai_client.aio.live.connect(model="gemini-2.5-flash-native-audio-preview-12-2025", config=config) as session:
            call_sid = None
            stream_sid = None

            async def receive_from_twilio():
                nonlocal call_sid, stream_sid
                try:
                    async for message in websocket.iter_text():
                        data = json.loads(message)
                        event = data.get('event')

                        if event == "start":
                            call_sid = data['start']['callSid']
                            stream_sid = data['start']['streamSid']
                        
                        elif event == "media":
                            audio_bytes = base64.b64decode(data['media']['payload'])
                            # FIXED: Properly passing the Blob object as a keyword argument
                            await session.send_realtime_input(
                                media=types.Blob(data=audio_bytes, mime_type="audio/pcm;rate=8000")
                            )
                            
                        elif event == "stop":
                            break
                except Exception as e:
                    logger.error(f"Error reading from Twilio: {e}")

            async def send_to_twilio():
                try:
                    while True:
                        async for response in session.receive():
                            
                            # 1. Handle AI Audio Output
                            if response.server_content and response.server_content.model_turn:
                                for part in response.server_content.model_turn.parts:
                                    if part.inline_data and part.inline_data.data:
                                        payload = base64.b64encode(part.inline_data.data).decode('utf-8')
                                        await websocket.send_json({
                                            "event": "media",
                                            "streamSid": stream_sid,
                                            "media": {"payload": payload}
                                        })
                            
                            # 2. Handle the Transfer Tool Call
                            if response.tool_call:
                                for call in response.tool_call.function_calls:
                                    if call.name == "transfer_to_human":
                                        reason = "user_request"
                                        initiate_transfer(call_sid, reason)
                                        await websocket.close()
                                        return 
                except Exception as e:
                    logger.error(f"Error receiving from Gemini: {e}")

            await asyncio.gather(receive_from_twilio(), send_to_twilio())

    except Exception as e:
        logger.error(f"Failed to connect to Gemini Live API: {e}")
    finally:
        if websocket.client_state.name != 'DISCONNECTED':
            await websocket.close()
        logger.info("WebSocket connection closed and resources cleaned up.")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")