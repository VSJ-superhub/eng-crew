from .base_specialist import SpecialistAgent


class GenericAgent(SpecialistAgent):
    agent_type = "generic"
    file_extensions: list[str] = []
