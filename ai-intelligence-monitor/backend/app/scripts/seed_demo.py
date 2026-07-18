from app.core.database import SessionLocal
from app.core.settings import settings
from app.coverage.service import ensure_coverage


def main() -> None:
    with SessionLocal() as session:
        topic, thesis = ensure_coverage(session, settings.coverage_path)
        session.commit()
        print(f"seeded coverage={topic.coverage_id} coverage_version={topic.version} thesis_version={thesis.version}")


if __name__ == "__main__":
    main()

