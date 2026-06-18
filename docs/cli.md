# Grid CLI

```
grid                                  # overview: your grid, endpoint, live models, next steps
grid --help   ·   grid <command> --help
grid version
```

## Grid

```
grid up [name]                        # bring a grid online (creates it on first run; default: home)
grid down [name]                      # take a grid offline (config persists; `up` brings it back)
grid ls                               # list your grids
grid info [grid] [--json]             # endpoint, key, live models
grid info [grid] --env                # print OPENAI_* exports
```

## Engines

```
grid join [grid]                      # join this box's auto-detected engine
grid join [grid] --at <url> -m <model>... [--name <id>]
grid join [grid] --serve <model>      # start the built-in engine, then join
grid join [grid] --media [--bundle <bundle>]...
grid join [grid] --dry-run            # show detected engines; register nothing
grid leave [grid] [--engine <id>] [--all]
```

## Models

```
grid models [grid] [--verbose]        # live models the grid can run now
grid catalog                          # models Grid can pull
grid pull <model>
grid rm <model> [--yes]
```

## Use

```
grid chat -m <model> "<message>"
grid image "<prompt>" [-o <dir>]
grid edit "<prompt>" -i <img>... [-o <dir>]
grid video "<prompt>" -i <img> [-o <dir>]
```

## Engine setup

```
grid engine install <name>            # llama.cpp (text) · comfyui (media)
grid engine pull <bundle>             # media bundle (comfyui)
grid engine status [--port <p>]       # built-in media engine (comfyui) status
grid engine start [--port <p>] [--detach]
grid engine stop
```

## Conventions

```
[grid] defaults to your only grid (or `home`); name it only when you have several
aliases:  ls = list   ·   rm = remove
output nouns: grid_url, openai_base_url, engines  (never provider/consumer/signaling)
```
