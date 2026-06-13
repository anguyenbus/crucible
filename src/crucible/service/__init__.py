"""
Crucible service layer (the migrating eval service).

Mirrors the monorepo ``app/<dir>/`` layout one-to-one. May import the kernel
package (absolute) and third-party infra; must NEVER import the non-migrating
demo/CLI layer.
"""
