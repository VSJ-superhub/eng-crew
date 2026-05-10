from pydantic import BaseModel
from datetime import datetime

class HealthStatus(BaseModel):
    uptime_seconds: int
    db_status: str
    timestamp: datetime
