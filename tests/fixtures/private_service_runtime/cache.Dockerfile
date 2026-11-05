# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
FROM busybox:1.37.0@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e
ARG PHASE
RUN --mount=type=cache,id=coordinare-probe-cache,target=/probe-cache,sharing=locked \
    if [ "$PHASE" = installer ]; then \
        printf retained >/probe-cache/marker; \
    else \
        test "$(cat /probe-cache/marker)" = retained; \
    fi
