# Yvonta - Body Factory

Your next life begins with an upload. 

This application is a Python-based web API designed to procedurally generate 3D bodies and avatars. It leverages a headless instance of Blender in combination with the MPFB2 (MakeHuman Plugin for Blender) plugin to construct these assets, with the entire pipeline containerized within Docker for seamless deployment.

---

## How to install?

Since the application is fully containerized, you do not need to install Blender or Python locally. Ensure you have **Docker** and **Docker Compose** installed on your system (e.g., Debian, Ubuntu, or your preferred Linux distribution).

1. **Clone the repository:**
```bash
   git clone [https://github.com/Yvonta/body-factory.git](https://github.com/Yvonta/body-factory.git)
   cd body-factory

```

2. **Build the Docker container:**
This step pulls the base image, installs Python dependencies, downloads Blender, and configures the MPFB2 plugin.
```bash
docker build -t yvonta-body-factory .

```


3. **Run the application:**
```bash
docker run -d -p 8090:8090 --name body-factory yvonta-body-factory

```


The API will now be accessible at `http://localhost:8090`.

---

## How to use?

The application exposes a RESTful web API that generates `.glb` 3D models from face images and applies fitted clothing to the generated avatars.

### 1. Generate an Avatar

Submit a face image alongside macro parameters (gender, age, weight) to generate the base body. The API returns the 3D `.glb` file and provides a unique `X-Avatar-Id` in the response headers for future modifications.

```bash
curl -sS -D headers.txt -o avatar.glb \
    -F "gender=0.0" \
    -F "age=0.4" \
    -F "weight=0.5" \
    -F "file=@path/to/face.jpg" \
    "http://localhost:8090/v1/avatar/generate"

```

*Check `headers.txt` for your `X-Avatar-Id`.*

### 2. Fit Clothing to the Avatar

Using the `X-Avatar-Id` returned in step 1, you can request specific garments. The API runs a headless Blender process to fit the clothing to your exact avatar's proportions and caches the result for near-instant subsequent requests.

```bash
curl -sS -o clothing_1.glb \
    "http://localhost:8090/v1/avatar/<YOUR_AVATAR_ID>/clothing/toigo_basic_tucked_t-shirt"

```

*Tip: You can load both `avatar.glb` and `clothing_1.glb` together into a viewer like [glTF Viewer](https://gltf-viewer.donmccurdy.com/) to verify that the garment lines up visually with the body's skeleton and bind pose.*

**API Endpoints:**

* `POST /v1/avatar/generate` - Generate a base body from an image and body parameters.
* `GET /v1/avatar/{avatar_id}/clothing/{clothes_name}` - Generate and retrieve a `.glb` file of clothing fitted to a specific avatar.

---

## What is Yvonta?

Yvonta is an initiative dedicated to the architectures of digital afterlife, AI cloning, and digital resurrection. Operating as both a news perspective platform and a technology hub, Yvonta specializes in custom software development, real-time 3D avatars, and persona replication. This repository is a foundational piece of that ecosystem, automating the physical creation of digital vessels.

---

## License

This project is licensed under the **CC0 1.0 Universal (CC0 1.0) Public Domain Dedication**.

You can copy, modify, distribute, and perform the work, even for commercial purposes, all without asking permission.
