from .bot import create_council_bot
from ..base import RoleDescriptor, register

for i in (1, 2, 3):
    role_key = f"council_{i}"
    register(RoleDescriptor(role_key=role_key, create_bot=lambda deps, rk=role_key: create_council_bot(deps, rk)))
