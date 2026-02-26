from .bot import create_elder_bot
from ..base import RoleDescriptor, register

_d = RoleDescriptor(role_key="elder", create_bot=create_elder_bot)
register(_d)
