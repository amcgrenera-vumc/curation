FROM alpine:3.3

MAINTAINER Taha Abdul-Basser <ta2471@cumc.columbia.edu>

RUN apk add --update \
    python \
    python-dev \
    py-pip \
    build-base \
    bash \
  && pip install virtualenv \
  && rm -rf /var/cache/apk/*

# install python libraries
RUN pip install requests \
  && pip install Flask \
  && pip install python-dateutil \
  && pip install Flask-Cache

# copy working-dir-context files to image
ADD data_steward /app

# Define default command.
CMD ["/bin/bash"]                                                              