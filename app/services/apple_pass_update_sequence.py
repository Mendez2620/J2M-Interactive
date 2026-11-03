from app.extensions import db
from app.models import ApplePassUpdateSequence


def allocate_apple_update_tag():
    """Allocate a collection-wide PassKit marker in the caller's transaction."""
    sequence = (
        ApplePassUpdateSequence.query
        .filter_by(id=1)
        .with_for_update()
        .one_or_none()
    )
    # db.create_all()-based isolated tests do not run Alembic's singleton seed.
    if sequence is None:
        sequence = ApplePassUpdateSequence(id=1, value=0)
        db.session.add(sequence)
        db.session.flush()
    sequence.value += 1
    db.session.flush()
    return sequence.value
