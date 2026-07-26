"""Resident-type membership sets — the town's two *different* boundaries.

``resident_type`` is a bare ``String(20)`` (``app/models/resident.py:53``, no
enum / no CHECK). Ten reads compare against it, and those ten are **not** one
semantic family: the moment player-authored (UGC) residents stop being typed
``"npc"``, the political reads must narrow while the population reads must not.

``Resident.is_autonomous`` already collapsed all ten onto a single predicate,
which is precisely the latent regression this module exists to split back
apart. The hybrid keeps its name and its population meaning; a second hybrid,
``Resident.is_civic_voter``, carries the political one.

``CIVIC_VOTER_TYPES``
    Political rights — who may cast a civic vote, who counts in the quorum
    denominator, who may stand for mayor. Narrow by default; adding a type here
    hands that type the ballot.

``SIM_RESIDENT_TYPES``
    World population — who is a simulated inhabitant at all: is ticked by the
    agent loop, shows on the town-hall roster, can hold a duty (labour, not
    politics), is swept by mayor-meta maintenance, joins the lecture debate
    pool. Broad by default; removing a type here makes that type silently
    vanish from the simulation.

The two sets are deliberately *not* nested by construction, but today
``CIVIC_VOTER_TYPES ⊂ SIM_RESIDENT_TYPES`` holds: a voter is always an
inhabitant.

Values (all live literals, no migration):

- ``"npc"``      built-in autonomous cast (``seed/preset_characters.py``,
                 ``origin="preset"``) — has political rights
- ``"resident"`` player-authored resident via forge / import — inhabitant,
                 **no** political rights
- ``"player"``   the user's own single avatar (``onboarding_service.py``) —
                 deliberately absent from *both* sets, and governed by a third,
                 untouched predicate family (``!= "player"``)
- ``"preset"``   admin-created resident (``schemas/admin.py:129`` default) —
                 absent from both sets, matching pre-hotfix behaviour exactly
"""

#: A-class reads (3): political rights. Widening this hands out the ballot.
CIVIC_VOTER_TYPES = frozenset({"npc"})

#: B/C-class reads (10): world population & ops sweeps. Narrowing this makes
#: residents silently disappear from the simulation.
#:
#: ``"resident"`` must be added here in the *same commit* that starts writing
#: it at the creation paths — an intermediate state where UGC residents are
#: typed ``"resident"`` but the population set still says ``{"npc"}`` would
#: erase them from the agent loop, the town-hall roster, the duty lookup and
#: the mayor sweeps.
SIM_RESIDENT_TYPES = frozenset({"npc", "resident"})

#: The type given to player-authored (forge / import) residents. Satisfies the
#: untouched ``!= "player"`` predicate family by construction, so world
#: presence (map, home-decor ownership, purge-candidacy) is unchanged.
UGC_RESIDENT_TYPE = "resident"

__all__ = ["CIVIC_VOTER_TYPES", "SIM_RESIDENT_TYPES", "UGC_RESIDENT_TYPE"]
