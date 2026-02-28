# 🎙️ai-voice-assistant

A real-time, ultra-low latency conversational AI phone assistant built with **Twilio Media Streams** and the **Gemini 2.0 Multimodal Live API**. 

This project demonstrates how to bridge traditional PSTN telephony with state-of-the-art AI, featuring full-duplex audio (allowing users to interrupt the AI) and **Tool Use / Function Calling** to perform a live handoff to a human agent.

## Features
* **Sub-second Latency:** Bypasses traditional STT/TTS pipelines by streaming raw audio directly to Gemini 2.0 Flash.
* **Interruption Handling:** Full-duplex WebSocket architecture allows the user to speak over the AI natively.
* **Intelligent Human Handoff:** Uses Gemini's Function Calling to detect frustration or explicit requests for a human, dynamically updating the active Twilio call via REST API to dial a live agent.

## Architecture

1. **Inbound Call:** Twilio receives a call and hits our `/voice` webhook.
2. **WebSocket Bridge:** Our FastAPI server responds with TwiML `<Connect><Stream>`, opening a bidirectional WebSocket.
3. **AI Processing:** Raw audio is piped to the Gemini Live API. Gemini streams audio responses back.
4. **The Handoff:** If Gemini triggers the `transfer_to_human` tool, our server issues a Twilio REST API command (`client.calls.update`) to pivot the call to a human number.

---

## Getting Started

### Prerequisites
* Python 3.9+
* A [Twilio Account](https://www.twilio.com/try-twilio) with a provisioned phone number.
* A [Google Gemini API Key](https://aistudio.google.com/app/apikey).
* [ngrok](https://ngrok.com/) (for local development exposing).

### 1. Local Setup
Clone the repository and install the dependencies:
```bash
git clone [https://github.com/harshal-rembhotkar/ai-voice-assistant.git](https://github.com/harshal-rembhotkar/ai-voice-assistant.git)
cd ai-voice-assistant
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
