"""Repository layer for the friends feature. All its SQLAlchemy queries live here."""
from typing import List, Optional, Tuple

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from friends_models import STATUS_ACCEPTED, STATUS_PENDING, FriendInviteCode, Friendship
from models import User


def pair_key(user_a_id: int, user_b_id: int) -> str:
    low, high = sorted((user_a_id, user_b_id))
    return f"{low}:{high}"


class FriendshipRepository:
    def find_by_id(self, db: Session, friendship_id: int) -> Optional[Friendship]:
        return db.get(Friendship, friendship_id)

    def find_between(self, db: Session, user_a_id: int, user_b_id: int) -> Optional[Friendship]:
        return db.query(Friendship).filter(Friendship.pair_key == pair_key(user_a_id, user_b_id)).first()

    def are_friends(self, db: Session, user_a_id: int, user_b_id: int) -> bool:
        friendship = self.find_between(db, user_a_id, user_b_id)
        return friendship is not None and friendship.status == STATUS_ACCEPTED

    def create_request(self, db: Session, requester_id: int, addressee_id: int) -> Optional[Friendship]:
        """Returns None if a row for this pair was created concurrently."""
        friendship = Friendship(
            requester_id=requester_id,
            addressee_id=addressee_id,
            status=STATUS_PENDING,
            pair_key=pair_key(requester_id, addressee_id),
        )
        db.add(friendship)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return None
        db.refresh(friendship)
        return friendship

    def accept(self, db: Session, friendship: Friendship) -> None:
        friendship.status = STATUS_ACCEPTED
        db.commit()

    def delete(self, db: Session, friendship: Friendship) -> None:
        db.delete(friendship)
        db.commit()

    def pending_incoming(self, db: Session, user_id: int) -> List[Tuple[Friendship, User]]:
        return (
            db.query(Friendship, User)
            .join(User, User.id == Friendship.requester_id)
            .filter(Friendship.addressee_id == user_id, Friendship.status == STATUS_PENDING)
            .order_by(Friendship.id)
            .all()
        )

    def pending_outgoing(self, db: Session, user_id: int) -> List[Tuple[Friendship, User]]:
        return (
            db.query(Friendship, User)
            .join(User, User.id == Friendship.addressee_id)
            .filter(Friendship.requester_id == user_id, Friendship.status == STATUS_PENDING)
            .order_by(Friendship.id)
            .all()
        )

    def accepted_friends(self, db: Session, user_id: int) -> List[User]:
        rows = (
            db.query(Friendship)
            .filter(
                Friendship.status == STATUS_ACCEPTED,
                (Friendship.requester_id == user_id) | (Friendship.addressee_id == user_id),
            )
            .all()
        )
        friend_ids = [row.addressee_id if row.requester_id == user_id else row.requester_id for row in rows]
        if not friend_ids:
            return []
        return db.query(User).filter(User.id.in_(friend_ids)).order_by(User.username).all()


class InviteCodeRepository:
    def find_for_user(self, db: Session, user_id: int) -> Optional[FriendInviteCode]:
        return db.query(FriendInviteCode).filter(FriendInviteCode.user_id == user_id).first()

    def find_by_code(self, db: Session, code: str) -> Optional[FriendInviteCode]:
        return db.query(FriendInviteCode).filter(FriendInviteCode.code == code).first()

    def save(self, db: Session, user_id: int, code: str) -> bool:
        """Create or replace the user's code. Returns False on a uniqueness clash."""
        row = self.find_for_user(db, user_id)
        if row is None:
            db.add(FriendInviteCode(user_id=user_id, code=code))
        else:
            row.code = code
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return False
        return True
