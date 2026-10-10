from datetime import datetime
from fastapi.middleware.cors import CORSMiddleware

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form
from fastapi.responses import FileResponse
import os
import uuid

import subprocess
import sys

from pydantic import BaseModel
from sqlalchemy import and_, or_, text

from .database import Base, SessionLocal, engine
from . import models


app = FastAPI(title="Gesture Chat")

DRAWING_FOLDER = "drawings"
os.makedirs(DRAWING_FOLDER, exist_ok=True)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create database tables if they don't exist yet.
Base.metadata.create_all(bind=engine)
with engine.begin() as connection:

    try:
        connection.execute(
            text(
                "ALTER TABLE messages "
                "ADD COLUMN message_type VARCHAR "
                "DEFAULT 'text'"
            )
        )
    except Exception:
        pass

    try:
        connection.execute(
            text(
                "ALTER TABLE messages "
                "ADD COLUMN file_path VARCHAR"
            )
        )
    except Exception:
        pass


class ChatMessage(BaseModel):
    receiver_id: int
    message: str


class ChatMessageResponse(BaseModel):
    sender_id: int
    message: str


class ConnectionManager:

    def __init__(self):
        self.active_connections = {}

    async def connect(self, user_id, websocket):
        await websocket.accept()
        self.active_connections[user_id] = websocket

    def disconnect(self, user_id):
        if user_id in self.active_connections:
            del self.active_connections[user_id]

    def is_online(self, user_id):
        return user_id in self.active_connections

    def get_connection(self, user_id):
        return self.active_connections.get(user_id)

    async def send_message(self, receiver_id, sender_id, message):
        websocket = self.get_connection(receiver_id)

        if websocket:
            response = ChatMessageResponse(
                sender_id=sender_id,
                message=message
            )

            await websocket.send_json(response.model_dump())


manager = ConnectionManager()


@app.get("/")
def home():
    return {"message": "Hello Gesture Chat 🚀"}


@app.get("/conversations/{user_a}/{user_b}")
def get_conversation(user_a: int, user_b: int):

    with SessionLocal() as db:
        messages = (
            db.query(models.Message)
            .filter(
                or_(
                    and_(
                        models.Message.sender_id == user_a,
                        models.Message.receiver_id == user_b
                    ),
                    and_(
                        models.Message.sender_id == user_b,
                        models.Message.receiver_id == user_a
                    )
                )
            )
            .order_by(models.Message.id.asc())
            .all()
        )

        return [
            {
                "id": item.id,
        "sender_id": item.sender_id,
        "receiver_id": item.receiver_id,
        "message": item.message,
        "message_type": item.message_type,
        "file_path": item.file_path,
        "created_at": (
            item.created_at.isoformat()
            if item.created_at else None
        )
            }
            for item in messages
        ]

@app.post("/drawings")
async def upload_drawing(file: UploadFile = File(...)):

    # Make sure a file was actually uploaded
    if not file.filename:
        return {"error": "No file selected"}

    # Generate a unique filename
    filename = f"{uuid.uuid4()}.png"

    file_path = os.path.join(
        DRAWING_FOLDER,
        filename
    )

    # Read uploaded file
    contents = await file.read()

    # Save it
    with open(file_path, "wb") as drawing_file:
        drawing_file.write(contents)

    return {
        "message": "Drawing uploaded successfully",
        "filename": filename,
        "path": file_path
    }

@app.get("/drawings/{filename}")
def get_drawing(filename: str):

    file_path = os.path.join(
        DRAWING_FOLDER,
        filename
    )

    if not os.path.exists(file_path):
        return {
            "error": "Drawing not found"
        }

    return FileResponse(
        file_path,
        media_type="image/png"
    )

@app.post("/drawings/send")
async def send_drawing(
    sender_id: int = Form(...),
    receiver_id: int = Form(...),
    file: UploadFile = File(...)
):

    if not file.filename:
        return {"error": "No drawing selected"}

    if file.content_type != "image/png":
        return {
            "error": "Only PNG drawings are supported"
        }

    # Generate unique filename
    filename = f"{uuid.uuid4()}.png"

    file_path = os.path.join(
        DRAWING_FOLDER,
        filename
    )

    # Read image
    contents = await file.read()

    # Save image
    with open(file_path, "wb") as drawing_file:
        drawing_file.write(contents)

    # Save drawing as a chat message
    with SessionLocal() as db:

        drawing_message = models.Message(
            sender_id=sender_id,
            receiver_id=receiver_id,
            message="[Drawing]",
            message_type="drawing",
            file_path=filename
        )

        db.add(drawing_message)
        db.commit()

        drawing_id = drawing_message.id

    # Notify receiver if they are online
    websocket = manager.get_connection(receiver_id)

    if websocket:

        await websocket.send_json({
            "type": "drawing",
            "sender_id": sender_id,
            "message_id": drawing_id,
            "filename": filename,
            "url": f"/drawings/{filename}"
        })

    return {
        "message": "Drawing sent successfully",
        "message_id": drawing_id,
        "filename": filename,
        "url": f"/drawings/{filename}"
    }

@app.post("/start-drawing")
def start_drawing(
    sender_id: int = Form(...),
    receiver_id: int = Form(...)
):
    subprocess.Popen([
        sys.executable,
        "hand_tracking.py",
        str(sender_id),
        str(receiver_id)
    ])

    return {
        "message": "Drawing started"
    }

@app.post("/drawings/send-saved")
def send_saved_drawing(
    sender_id: int = Form(...),
    receiver_id: int = Form(...)
):
    file_path = "drawing.png"

    if not os.path.exists(file_path):
        raise HTTPException(
            status_code=404,
            detail="drawing.png not found"
        )

    filename = f"{uuid.uuid4()}.png"
    destination = os.path.join(DRAWING_FOLDER, filename)

    with open(file_path, "rb") as source:
        with open(destination, "wb") as target:
            target.write(source.read())

    db = SessionLocal()

    new_message = models.Message(
        sender_id=sender_id,
        receiver_id=receiver_id,
        message="[Drawing]",
        message_type="drawing",
        file_path=filename
    )

    db.add(new_message)
    db.commit()
    db.refresh(new_message)

    receiver_socket = manager.get_connection(receiver_id)

    if receiver_socket:
        import asyncio

        asyncio.create_task(
            receiver_socket.send_json({
                "type": "drawing",
                "sender_id": sender_id,
                "message_id": new_message.id,
                "filename": filename,
                "url": f"/drawings/{filename}"
            })
        )

    db.close()

    return {
        "message": "Drawing sent",
        "filename": filename
    }

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(
    websocket: WebSocket,
    user_id: int
):
    await manager.connect(user_id, websocket)

    try:
        while True:
            data = await websocket.receive_json()
            chat_message = ChatMessage(**data)

            # Save the message before attempting delivery.
            with SessionLocal() as db:
                db_message = models.Message(
                    sender_id=user_id,
                    receiver_id=chat_message.receiver_id,
                    message=chat_message.message
                )

                db.add(db_message)
                db.commit()

            print(
                f"User {user_id} says: "
                f"{chat_message.message} "
                f"to {chat_message.receiver_id}"
            )

            # Deliver instantly if the receiver is online.
            await manager.send_message(
                chat_message.receiver_id,
                user_id,
                chat_message.message
            )

    except WebSocketDisconnect:
        manager.disconnect(user_id)