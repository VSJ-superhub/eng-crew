from .base_specialist import SpecialistAgent


class DatabaseAgent(SpecialistAgent):
    agent_type = "database"
    file_extensions: list[str] = [".py", ".sql"]
