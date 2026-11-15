## Plan of adopting the dcpy-self built-in features

1. C2 finish — the COMMANDS flip: exporter fixture converts fully, then COMMANDS/ArgSpec/_parse_invocation/_convert_arg/render_help/_register_self_command retire	The breaking change
2. C3/C4 — remaining framework verbs migrate; the 922-line hub bridge retires	Medium
3. on_http_throttle bridge → RateLimitManager	Small
4. Per-route latency EWMA (http_route_latencies())	Small
5. Bounded probe-down recovery	Small
6. cache_stats() via ControlAPI

## Removal plan

1. Note the functionality into a temporary doc before surgical removal of each method.
2. Note the lines where these methods are located beforehand and create checkpoints, then utilize the write tool to target these checkpoints with precision so only related code gets replaced.
3. Reflect changes to the tests if preservation is possible then do so by wiring them into the new adopted methods, unrelated tests retires.
4. After removal run related test only, when everything is green start the implementation process, do not introduce custom method when its available in the library unless it constraints any feature.
