"""Cross-file usage."""
from alive import used_alive
from pkg import mod as mod_alias
from pkg.mod import Greeter
from worker import Worker

print(used_alive())

worker = Worker()
worker.live_method()

print(mod_alias.public_api())
print(Greeter().greet())
