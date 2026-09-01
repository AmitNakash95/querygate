# ClusterFuzzLite build image.
#
# Builds the fuzz targets in fuzz/ against the OSS-Fuzz Python base image,
# pinned by digest like every other image in this repository. Pinning the
# fuzzing toolchain would be a liability if it froze — Atheris and the libFuzzer
# runtime both matter — so this digest is kept current by Dependabot's `docker`
# ecosystem (.github/dependabot.yml), which is what makes pinning safe here
# rather than merely tidy.
FROM gcr.io/oss-fuzz-base/base-builder-python@sha256:6b2b3a7e4a2da50de47f94925cffbe34747e1aae4f33d7f07ba6c681dd648b23

# The project source. $SRC is set by the base image.
COPY . $SRC/querygate
WORKDIR $SRC/querygate
COPY .clusterfuzzlite/build.sh $SRC/build.sh
