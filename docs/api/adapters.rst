adapters — Discord boundary
===========================

Client protocol
---------------
.. automodule:: adapters.base
   :members: DiscordClient, ClientFactory, DiscordMessage

Rich event model
----------------
.. automodule:: adapters.model
   :members:

Live adapter
------------
.. automodule:: adapters.discord.client
   :members: RealDiscordClient, RealClientFactory

Simulator
---------
.. automodule:: adapters.simulator.world
   :members: SimWorld
.. automodule:: adapters.simulator.client
   :members: SimClient
.. automodule:: adapters.simulator.runner
   :members: SimulationRunner
.. automodule:: adapters.simulator.scenarios
   :members: Scenario, ScenarioAPI, SCENARIOS
