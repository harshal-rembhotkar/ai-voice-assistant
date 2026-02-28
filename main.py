import os
import json
import base64
import asyncio
import logging
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException
from fastapi.responses import HTMLResponse
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from google import genai
from google.genai import types
from dotenv import load_dotenv

# --- 1. Production Setup & Configuration ---
load_dotenv()  # Load environment variables from a .env file

# Configure structured logging
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

# Validate critical secrets
if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, GEMINI_API_KEY]):
    logger.critical("Missing critical environment variables. Check your .env file.")
    exit(1)

# Initialize Clients
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
genai_client = genai.Client(api_key=GEMINI_API_KEY, http_options={'api_version': 'v1alpha'})

app = FastAPI(title="Twilio-Gemini Voice Assistant")

# --- 2. The TwiML Webhook ---
@app.post("/voice")
async def handle_voice(request: Request):
    """Answers the incoming Twilio call and opens a WebSocket stream."""
    host = request.url.hostname
    # In production, ensure you are routing through wss:// (secure WebSockets)
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
    """Modifies the live Twilio call to dial a human agent."""
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
    """Handles the bi-directional audio stream between Twilio and Gemini."""
    await websocket.accept()
    logger.info("WebSocket connection established with Twilio.")
    
    # Configure Gemini's Persona and Tools
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
        # Open the connection to Gemini Multimodal Live API
        async with genai_client.live.connect(model="gemini-2.0-flash-exp", config=config) as session:
            call_sid = None
            stream_sid = None

            async def receive_from_twilio():
                """Reads raw audio from Twilio and sends it to Gemini."""
                nonlocal call_sid, stream_sid
                try:
                    async for message in websocket.iter_text():
                        data = json.loads(message)
                        event = data.get('event')

                        if event == "start":
                            call_sid = data['start']['callSid']
                            stream_sid = data['start']['streamSid']
                            logger.info(f"Stream started for Call SID: {call_sid}")
                        
                        elif event == "media":
                            # Twilio sends base64 encoded G.711 audio.
                            audio_bytes = base64.b64decode(data['media']['payload'])
                            await session.send(input=audio_bytes, end_of_turn=False)
                            
                        elif event == "stop":
                            logger.info("Twilio stream stopped by the caller.")
                            break
                except WebSocketDisconnect:
                    logger.warning("Twilio WebSocket disconnected unexpectedly.")
                except Exception as e:
                    logger.error(f"Error reading from Twilio: {e}")

            async def send_to_twilio():
                """Receives audio/tools from Gemini and sends to Twilio."""
                try:
                    async for response in session.receive():
                        # 1. Handle AI Audio Output
                        if response.audio:
                            payload = base64.b64encode(response.audio).decode('utf-8')
                            await websocket.send_json({
                                "event": "media",
                                "streamSid": stream_sid,
                                "media": {"payload": payload}
                            })
                        
                        # 2. Handle the Transfer Tool Call
                        if response.tool_call:
                            for call in response.tool_call.function_calls:
                                if call.name == "transfer_to_human":
                                    reason = call.args.get('reason', 'user_request')
                                    initiate_transfer(call_sid, reason)
                                    # Close the WebSocket so Twilio processes the new TwiML
                                    await websocket.close()
                                    return # Exit the loop
                except Exception as e:
                    logger.error(f"Error receiving from Gemini: {e}")

            # Run both streams concurrently
            await asyncio.gather(receive_from_twilio(), send_to_twilio())

    except Exception as e:
        logger.error(f"Failed to connect to Gemini Live API: {e}")
    finally:
        # Ensure cleanup
        if websocket.client_state.name != 'DISCONNECTED':
            await websocket.close()
        logger.info("WebSocket connection closed and resources cleaned up.")

if __name__ == "__main__":
    import uvicorn
    # Use production standard server runner
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")
