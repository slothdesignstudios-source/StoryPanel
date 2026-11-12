"""owner write guard takes FOR KEY SHARE

Revision ID: 0009
Revises: 0008

FOR SHARE on the owner row conflicted with ordinary UPDATEs of users (credit holds), so
concurrent work for one user deadlocked. FOR KEY SHARE is the lock a foreign-key check
already takes: it does not conflict with those updates, but it does conflict with the
SELECT ... FOR UPDATE that account deletion takes first, so deletion still waits for
in-flight inserts and later inserts see deleted_at.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FN = """
create or replace function forbid_writes_for_deleted_owner() returns trigger as $$
declare gone timestamptz;
begin
  select deleted_at into gone from users where id = new.owner_id for {lock};
  if gone is not null then
    raise exception 'account deleted' using errcode = 'P0001', hint = 'account_deleted';
  end if;
  return new;
end $$ language plpgsql"""


def upgrade() -> None:
    op.execute(_FN.format(lock="key share"))


def downgrade() -> None:
    op.execute(_FN.format(lock="share"))
