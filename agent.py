from __future__ import annotations

import asyncio
import logging
import sys
from dotenv import load_dotenv
import json
import os
from typing import Any

from livekit import rtc, api
from livekit.agents import (
    AgentSession,
    Agent,
    JobContext,
    room_io,
    JobProcess,
    function_tool,
    RunContext,
    get_job_context,
    cli,
    WorkerOptions,
    TurnHandlingOptions,
)
from livekit.plugins import (
    deepgram,
    openai,
    cartesia,
    silero,
    noise_cancellation,  # noqa: F401
)
from livekit.plugins.turn_detector.english import EnglishModel

from mcp_bridge.lib.config import ensure_worker_env, get_agent_name, get_llm_model

load_dotenv(dotenv_path=".env")
logger = logging.getLogger("clauver-general-agent")
logger.setLevel(logging.INFO)


class OutboundCaller(Agent):
    def __init__(
        self,
        *,
        boss: str,
        task: str,
        dial_info: dict[str, Any],
        target_name: str | None = None,
    ):
        super().__init__(
            instructions=f"""
            You are Clauver, a warm, clear, professional voice assistant for {boss}.
            {boss} has a voice condition, so you make phone calls and deliver messages on his behalf.
            This is a real phone call: be concise, natural, and calm. No emojis.

            Task (ground truth of the message):
            "{task}"

            Your job:
            - Deliver this task message on {boss}'s behalf, without changing its meaning.
            - Give the other person a chance to reply.
            - Pass their reply back to {boss}.
            - End the call politely by thanks and goodbye.

            Call behaviour:
            - Start politely and clearly.
            - If the other person's name is known ({target_name if target_name else "not provided"}), use it once near the start.
            - Clearly say you are calling on behalf of {boss}.
            - Keep your first full reply after the greeting to ONE short sentence. Add detail only after they respond.
            - If asked, say you are an AI assistant helping {boss} because he has a voice condition.
            - Do not pretend to be {boss}.

            Message delivery:
            - State {boss}'s message clearly and naturally.
            - Then ask once if they would like you to pass anything back to {boss}.
            - WAIT for their answer before treating the task as complete.
            - If they give a reply, say thanks I'll pass that along to {boss}.
            - If they have nothing to add, acknowledge that and move to closing.
            - Keep the conversation short, warm, and respectful.

            Tools:
            - Use `save_result` only after:
            - you have delivered the message, and
            - the other person has replied (or clearly has nothing to add).
            - Do NOT say a goodbye or summary before calling it — `save_result` handles closing.
            - Use `handle_voicemail` if you reach voicemail or a beep.
            - Use `transfer_call` only if they clearly want to speak to a human urgently.
            - Use `end_call` only if `save_result` is not appropriate and you have already spoken thanks and goodbye.

            General rules:
            - Let them finish speaking.
            - If they interrupt, stop and listen.
            - Never invent facts that were not said on the call.
            """
        )

        self.participant: rtc.RemoteParticipant | None = None
        self.dial_info = dial_info
        self.boss = boss
        self.task = task
        self.target_name = target_name
        self._save_result_recorded = False
        self._goodbye_spoken = False
        self._closing_in_progress = False
        self._hangup_completed = False
        self._call_ended = False
        self.call_result: dict[str, Any] = {
            "status": "unknown",
            "outcome": None,
            "details": [],
        }

    def set_participant(self, participant: rtc.RemoteParticipant):
        self.participant = participant

    async def hangup(self):
        """Hang up by deleting the room. Idempotent after a successful delete."""
        if self._hangup_completed:
            return

        job_ctx = get_job_context()
        await asyncio.sleep(1)
        try:
            await job_ctx.api.room.delete_room(
                api.DeleteRoomRequest(
                    room=job_ctx.room.name,
                )
            )
        except Exception as e:
            logger.error("error hanging up call: %s", e)
            raise

        self._hangup_completed = True
        self._call_ended = True

    @function_tool()
    async def transfer_call(self, ctx: RunContext):
        """Transfer the call to a human after the other person clearly asks for it and confirms they want to be transferred."""
        transfer_to = self.dial_info.get("transfer_to")
        if not transfer_to:
            return "No transfer number is available."

        logger.info(f"transferring call to {transfer_to}")

        await ctx.session.generate_reply(
            instructions="Briefly let the person know you are transferring them now."
        )

        job_ctx = get_job_context()
        try:
            await ctx.wait_for_playout()
            await job_ctx.api.sip.transfer_sip_participant(
                api.TransferSIPParticipantRequest(
                    room_name=job_ctx.room.name,
                    participant_identity=self.participant.identity,
                    transfer_to=f"tel:{transfer_to}",
                )
            )
            logger.info(f"transferred call to {transfer_to}")
            return "Call transferred successfully."
        except Exception as e:
            logger.error(f"error transferring call: {e}")
            await ctx.session.generate_reply(
                instructions="Apologise briefly and say the transfer did not work."
            )
            await ctx.wait_for_playout()
            await self.hangup()
            return "Transfer failed."

    @function_tool()
    async def end_call(self, ctx: RunContext):
        """Final tool before hanging up. Use only after your final summary, thanks, and goodbye have been spoken."""
        logger.info(f"ending the call for {self.participant.identity}")

        await ctx.wait_for_playout()
        await asyncio.sleep(1)
        try:
            await self.hangup()
        except Exception:
            return "Hangup failed; cleanup pending."
        return "Call ended."

    @function_tool()
    async def handle_voicemail(self, ctx: RunContext):
        """Called when the call reaches voicemail or an answering machine beep."""
        logger.info(f"detected voicemail for {self.participant.identity}")

        msg_handle = await ctx.session.generate_reply(
            instructions=f"""
            Leave a short voicemail on behalf of {self.boss}.
            State your name, say you are calling on behalf of {self.boss}, mention the purpose briefly,
            ask them to call back if appropriate, then say thank you and goodbye.
            Keep it short.
            """
        )

        if msg_handle:
            await msg_handle.wait_for_playout()

        await asyncio.sleep(0.8)
        await self.hangup()
        return "Voicemail handled."

    @function_tool()
    async def save_result(
        self,
        ctx: RunContext,
        status: str,
        outcome: str,
        details: str = "",
    ):
        """Save the result of the call once the task is complete or clearly blocked.

        Args:
            status: success, failed, voicemail, transferred, or follow_up_needed
            outcome: short summary of what happened
            details: any key details such as time, date, address, booking info, callback request, or next step
        """
        logger.info(
            f"saving result for {self.participant.identity}: status={status}, outcome={outcome}, details={details}"
        )

        if self._hangup_completed:
            return "Call already ended."

        if self._closing_in_progress:
            return (
                "Closing already in progress; wait for the current goodbye "
                "and hangup to finish."
            )

        if self._save_result_recorded:
            try:
                await self.hangup()
            except Exception:
                return (
                    "Result already recorded; hangup failed; cleanup pending "
                    "(try end_call)."
                )
            return "Call ended."

        self._save_result_recorded = True
        self._closing_in_progress = True
        try:
            self.call_result = {
                "status": status,
                "outcome": outcome,
                "details": details,
            }

            speech_error: BaseException | None = None
            hangup_error: BaseException | None = None
            try:
                if not self._goodbye_spoken:
                    self._goodbye_spoken = True
                    try:
                        reply_handle = await ctx.session.say(
                            f"Thanks for your time — I'll pass that along to {self.boss}. Take care, bye!",
                            allow_interruptions=False,
                        )
                        if reply_handle:
                            await reply_handle.wait_for_playout()
                    except BaseException as e:
                        speech_error = e
                        logger.error(
                            "error speaking goodbye after save_result: %s", e
                        )
            finally:
                try:
                    await asyncio.sleep(0.8)
                    if not self._hangup_completed:
                        await asyncio.shield(self.hangup())
                except BaseException as e:
                    hangup_error = e
                    logger.error("error hanging up after save_result: %s", e)

            if hangup_error or not self._hangup_completed:
                if isinstance(speech_error, asyncio.CancelledError) or isinstance(
                    hangup_error, asyncio.CancelledError
                ):
                    return (
                        "Result recorded; cleanup did not complete due to cancellation."
                    )
                if speech_error and hangup_error:
                    return (
                        "Result recorded, but goodbye speech and hangup failed; "
                        "cleanup pending (try end_call)."
                    )
                return (
                    "Result recorded, but hangup failed; cleanup pending (try end_call)."
                )

            if isinstance(speech_error, asyncio.CancelledError):
                return (
                    "Result recorded and call ended, but goodbye was cancelled "
                    "before it finished."
                )
            if speech_error:
                return (
                    "Result recorded and call ended, but goodbye speech failed "
                    "before hangup."
                )
            return "Result saved and call ended."
        finally:
            self._closing_in_progress = False

async def entrypoint(ctx: JobContext):
    outbound_trunk_id = os.environ["SIP_OUTBOUND_TRUNK_ID"]

    logger.info(f"connecting to room {ctx.room.name}")
    await ctx.connect()

    dial_info = json.loads(ctx.job.metadata)
    # log to remove later
    logger.info(f"dispatch metadata: {ctx.job.metadata}")

    participant_identity = phone_number = dial_info["phone_number"]
    target_name = dial_info.get("target_name", None)
    boss = dial_info.get("boss") or os.environ.get("BOSS_NAME") or "Boss"
    task = dial_info.get(
        "task",
        f"Call on behalf of {boss}, introduce yourself clearly, and help with their request.",
    )

    agent = OutboundCaller(
        target_name=target_name,
        boss=boss,
        task=task,
        dial_info=dial_info,
    )

    session = AgentSession(
        # turn_detection=EnglishModel(),
        turn_handling=TurnHandlingOptions(
            turn_detection=EnglishModel(),
            # Updated to v1.5.0 format
            endpointing={
                "min_delay": 0.15,
                "max_delay": 0.8,
            },
            interruption={
                "enabled": False,
                "mode": "adaptive",
            },
        ),
        vad=ctx.proc.userdata["vad"],
        stt=deepgram.STT(),
        tts=cartesia.TTS(
            model="sonic-turbo",
            voice="a4a16c5e-5902-4732-b9b6-2a48efd2e11b",
        ),
        llm=openai.LLM(model=get_llm_model()),
    )

    session_started = asyncio.create_task(
        session.start(
            agent=agent,
            room=ctx.room,
            room_options=room_io.RoomOptions(
                audio_input=room_io.AudioInputOptions(
                    noise_cancellation=noise_cancellation.BVCTelephony(),
                    pre_connect_audio=True,
                    # auto_gain_control=True,
                    # pre_connect_audio_timeout=3.0,
                ),
            ),
        )
    )

    try:
        await ctx.api.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                room_name=ctx.room.name,
                sip_trunk_id=outbound_trunk_id,
                sip_call_to=phone_number,
                participant_identity=participant_identity,
                wait_until_answered=True,
            )
        )

        await session_started
        participant = await ctx.wait_for_participant(identity=participant_identity)
        logger.info(f"participant joined: {participant.identity}")
        agent.set_participant(participant)

        await session.say(
            f"Hi, {agent.target_name if agent.target_name else 'there'}, this is Clauver, I'm AI assistant calling on behalf of {boss}. One moment please while I pull up the info, thanks for your patience.",
            allow_interruptions=False,
        )

    except api.TwirpError as e:
        logger.error(
            f"error creating SIP participant: {e.message}, "
            f"SIP status: {e.metadata.get('sip_status_code')} "
            f"{e.metadata.get('sip_status')}"
        )
        ctx.shutdown()

def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()
    
if __name__ == "__main__":
    cli_args = sys.argv[1:]
    subcommand = cli_args[0] if cli_args else None
    skip_worker_env = (
        subcommand in (None, "download-files")
        or "--help" in cli_args
        or "-h" in cli_args
    )
    if not skip_worker_env and subcommand in ("start", "dev"):
        ensure_worker_env()
        get_llm_model()

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            agent_name=get_agent_name(),
            num_idle_processes=2,
        )
    )