variable "ARCH" {
  default = "amd64"
}
variable "BASE_IMAGE" {
  default = null
}
variable "SLIM_BASE" {
  default = null
}
variable "TRT_BASE" {
  default = null
}
variable "COMPUTE_LEVEL" {
  default = ""
}
variable "BASE_HOOK" {
  # Ensure an up-to-date python 3.11 is available in jetson images
  default = <<EOT
if grep -iq \"ubuntu\" /etc/os-release; then
  . /etc/os-release

  # Add the deadsnakes PPA repository
  echo "deb https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu $VERSION_CODENAME main" >> /etc/apt/sources.list.d/deadsnakes.list
  echo "deb-src https://ppa.launchpadcontent.net/deadsnakes/ppa/ubuntu $VERSION_CODENAME main" >> /etc/apt/sources.list.d/deadsnakes.list

  # Add deadsnakes signing key
  apt-key adv --keyserver keyserver.ubuntu.com --recv-keys F23C5A6CF475977595C89F51BA6932366A755776
fi
EOT
}

target "_build_args" {
  args = {
    BASE_IMAGE = BASE_IMAGE,
    SLIM_BASE = SLIM_BASE,
    TRT_BASE = TRT_BASE,
    COMPUTE_LEVEL = COMPUTE_LEVEL,
    BASE_HOOK = BASE_HOOK
  }
  platforms = ["linux/${ARCH}"]
}

target wget {
  dockerfile = "docker/main/Dockerfile"
  target = "wget"
  inherits = ["_build_args"]
}

target deps {
  dockerfile = "docker/main/Dockerfile"
  target = "deps"
  inherits = ["_build_args"]
}

target rootfs {
  dockerfile = "docker/main/Dockerfile"
  target = "rootfs"
  inherits = ["_build_args"]
}

target wheels {
  dockerfile = "docker/main/Dockerfile"
  target = "wheels"
  inherits = ["_build_args"]
}

target devcontainer {
  dockerfile = "docker/main/Dockerfile"
  platforms = ["linux/amd64"]
  target = "devcontainer"
}

target "trt-deps" {
  dockerfile = "docker/tensorrt/Dockerfile.base"
  context = "."
  contexts = {
    deps = "target:deps",
  }
  inherits = ["_build_args"]
}

target "tensorrt" {
  dockerfile = "docker/tensorrt/Dockerfile.${ARCH}"
  context = "."
  contexts = {
    wget = "target:wget",
    wheels = "target:wheels",
    deps = "target:deps",
    rootfs = "target:rootfs"
  }
  target = "frigate-tensorrt"
  inherits = ["_build_args"]
  # NOTE: cache_to=type=local needs a docker-container buildx driver; the
  # default `docker` driver rejects it. Cache config will be re-enabled once
  # we provision a `docker buildx create --driver docker-container` builder.
}

# Cache anchor: pre-warm the heavy stages explicitly. Run `make deps-cache`
# after requirements*.txt or build scripts change. Subsequent main builds
# pick up the cache automatically via the cache-from above.
target "deps-cache" {
  dockerfile = "docker/main/Dockerfile"
  target = "deps"
  inherits = ["_build_args"]
  tags = ["frigate-cache:deps"]
  output = ["type=docker"]
}

target "trt-wheels-cache" {
  dockerfile = "docker/tensorrt/Dockerfile.${ARCH}"
  context = "."
  contexts = {
    wheels = "target:wheels"
  }
  target = "trt-wheels"
  inherits = ["_build_args"]
  tags = ["frigate-cache:trt-wheels"]
  output = ["type=docker"]
}

group "deps-cache-all" {
  targets = ["deps-cache", "trt-wheels-cache"]
}

target "devcontainer-trt" {
  dockerfile = "docker/tensorrt/Dockerfile.amd64"
  context = "."
  contexts = {
    wheels = "target:wheels",
    trt-deps = "target:trt-deps",
    devcontainer = "target:devcontainer"
  }
  platforms = ["linux/amd64"]
  target = "devcontainer-trt"
}
