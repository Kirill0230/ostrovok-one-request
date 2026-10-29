from typing import Optional

from .models import AgentReply, FeedbackRequest
from .store import Session


def _bucket(total: int) -> int:
    return int(round(total, -4))


def track_reply(sess: Session, event: str, stage_before: str, reply: AgentReply,
                req: Optional[FeedbackRequest] = None, action_type: Optional[str] = None) -> None:
    s = sess.state
    if event == "message":
        sess.track("message", stage_before=stage_before, deck=reply.deck_kind, changes=len(reply.changes),
                   clarification=stage_before == "start" and not reply.cards and s.stage == "start")
    elif event == "feedback" and req is not None:
        sess.track("feedback", kind=req.kind, action=req.action, reason=req.reason,
                   destination=req.card_id if req.kind == "destination" else s.destination_id)
    elif event == "action":
        sess.track("action", type=action_type)
    elif event == "session_started":
        sess.track("session_started")

    if reply.cards:
        props = {"kind": reply.deck_kind, "n": len(reply.cards)}
        if reply.deck_kind == "destination":
            props["destinations"] = [c.id for c in reply.cards]
        sess.track("recommendations_shown", **props)
    if reply.changes and stage_before not in ("start",):
        sess.track("plan_changed", count=len(reply.changes), booked=stage_before == "booked")
    if s.stage == "review" and reply.ui_action == "open_cart" and reply.state:
        sess.track("cart_ready", destination=s.destination_id, total_bucket=_bucket(reply.state.total),
                   issues=len([i for i in reply.state.issues if i.severity != "info"]))
    if s.stage == "booked" and stage_before != "booked" and s.booking:
        t = s.travellers
        sess.track("booked", destination=s.destination_id, nights=s.nights, adults=t.adults, children=t.children,
                   pets=t.pets, total_bucket=_bucket(s.booking.total))
