# Sanhita, the certification workbench.
#
# Two things about this image are deliberate and worth reading before changing
# it.
#
# **The corpus is baked in.** Sanhita is a compiler, and a compiler with no
# source file is a blank screen. The SEBI stock broker master circular and the
# compiled rule store are copied into the image rather than mounted, so the
# container is self-sufficient and a reviewer who opens the URL sees a real
# rulebook rather than an empty state. They are about 12 MB together.
#
# **No third-party service is required at runtime.** No CDN, no font service, no
# model API, and no network call anywhere in the path that evaluates a rule.
# Every screen renders in a room with no network, and the deployment does not
# quietly undo that.
#
# There is exactly one outbound request in the whole product, and it is worth
# stating here rather than letting a reader discover it: "Check SEBI now" reads
# sebi.gov.in's own circulars listing when a person presses the button. Not on
# page load, not on a schedule, and nothing it returns enters the rulebook by
# being found. An image that claimed "nothing is fetched at runtime" while
# shipping that route was overstating the case.

FROM python:3.12-slim AS base

# PyMuPDF ships manylinux wheels, so no compiler is needed. libgl and libglib
# are its runtime shared libraries and are not in the slim image.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Dependencies first, so a code change does not reinstall the world.
COPY pyproject.toml ./
COPY src/ ./src/
RUN pip install --no-cache-dir ".[web]"

# The regulator's own PDF, the compiled rulebook, and the fonts. Fonts are
# copied explicitly because they are gitignored in the repository but the page
# must not fall back to a system face.
COPY corpus/ ./corpus/
COPY .sanhita/ ./.sanhita/
COPY ui/ ./ui/
# chmod is not belt and braces, it is the fix.
#
# `core.fileMode` is false on a Windows checkout, so git recorded this script
# as 100644 and never noticed. Docker building from a Windows context marks it
# executable anyway, so a deploy from that machine worked; CI checks out on
# Linux, gets 644, and the container died with "failed to spawn command:
# /app/docker-entrypoint.sh: Permission denied", restarted ten times and
# stopped. The mode is fixed in git as well, and this line means a future
# checkout on any platform cannot reintroduce it.
COPY --chmod=755 docker-entrypoint.sh ./docker-entrypoint.sh

# The regulation ships. Nobody's firm does.
#
# This used to run `demo-seed`, which built a synthetic firm, a filing
# register, a recorded assessment and an account, so a visitor opened the site
# on somebody else's compliance position. Labelling it helped and did not fix
# it: a jury judging a compliance product should meet the product, not a
# fixture, and every figure they saw belonged to a company that does not exist.
#
# So the image now carries SEBI's published circulars and nothing else. The
# stock broker master circular is copied in above, because a compiler with no
# source file is a blank screen. This adds the two Investment Adviser editions
# so the amendment comparison has both sides of a real reissue to open.
#
# No account, no company, no filing register, no assessment. Whoever opens the
# site records their own firm, which is the only honest starting point for a
# product that assesses firms.
RUN sanhita shelve-circulars  && echo "SEBI circulars shelved; no firm data in this image"

# A second, pristine copy at a path no host disk will be mounted over. The
# entrypoint restores from it when the live store has been shadowed empty.
# Taken after the circulars are shelved, so a restored store carries the
# rulebook and the editions to compare it against rather than a bare file.
RUN mkdir -p /app/.sanhita-dist && cp -R /app/.sanhita/. /app/.sanhita-dist/

# Sessions are signed with a key derived from this. Without it, sign-up
# refuses rather than issuing a cookie nobody can verify, which is correct but
# makes the deployment look broken. Set a real value in the host's secrets;
# this default only keeps a bare `docker run` usable.
ENV SANHITA_SIGNING_KEY=change-me-in-the-host-secrets \
    PORT=8000

# Not root. The container writes only to .sanhita, which it owns.
RUN useradd --create-home --uid 10001 sanhita \
 && chown -R sanhita:sanhita /app
USER sanhita

EXPOSE 8000

# A container that answers /healthz is one whose PDF parsed and whose store
# loaded. Anything less is not actually up.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
  CMD python -c "import urllib.request,os,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8000')+'/healthz',timeout=8).status==200 else 1)"

# The entrypoint expands $PORT and seeds the store first. Hosts assign the
# port; hard-coding 8000 is the most common reason a container starts and is
# never routed to.
CMD ["/app/docker-entrypoint.sh"]
