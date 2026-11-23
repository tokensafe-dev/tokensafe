"""
TokenSafe Greenfield Project Scaffolder.
Creates deterministic, token-efficient Fullstack, UI, and Backend projects
instrumented with tokensafe-ui, tokensafe-python, and the UARP/OUMV protocol.
"""

import os
import sys
import json
from typing import Dict, Any, Optional, List


DEFAULT_FRONTEND_PACKAGE_JSON = {
    "name": "{project_name}-ui",
    "version": "0.1.0",
    "private": True,
    "type": "module",
    "scripts": {
        "dev": "vite",
        "build": "vite build",
        "preview": "vite preview",
        "typecheck": "tsc --noEmit"
    },
    "dependencies": {
        "tokensafe-ui": "^0.1.6",
        "zod": "^3.24.1"
    },
    "devDependencies": {
        "typescript": "^5.7.2",
        "vite": "^6.0.7"
    }
}

DEFAULT_TSCONFIG = {
    "compilerOptions": {
        "target": "ES2022",
        "useDefineForClassFields": True,
        "module": "ESNext",
        "lib": ["ES2022", "DOM", "DOM.Iterable"],
        "skipLibCheck": True,
        "moduleResolution": "bundler",
        "allowImportingTsExtensions": True,
        "resolveJsonModule": True,
        "isolatedModules": True,
        "noEmit": True,
        "jsx": "preserve",
        "strict": True,
        "noUnusedLocals": True,
        "noUnusedParameters": True,
        "noFallthroughCasesInSwitch": True
    },
    "include": ["src"]
}

DEFAULT_VITE_CONFIG = """import { defineConfig } from 'vite';

export default defineConfig({
  server: {
    port: 5173,
    host: true,
  },
});
"""

DEFAULT_HTML = """<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>{project_name} | TokenSafe App</title>
    <link rel="stylesheet" href="./src/style.css" />
  </head>
  <body>
    <div id="app"></div>
    <script type="module" src="./src/main.ts"></script>
  </body>
</html>
"""

DEFAULT_STYLE_CSS = """* {
  box-sizing: border-box;
  margin: 0;
  padding: 0;
}

body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
  background-color: #0d1117;
  color: #c9d1d9;
  display: flex;
  justify-content: center;
  align-items: center;
  min-height: 100vh;
}

.container {
  background: #161b22;
  border: 1px solid #30363d;
  border-radius: 12px;
  padding: 2.5rem;
  width: 100%;
  max-width: 480px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.4);
}

h1 {
  font-size: 1.75rem;
  font-weight: 600;
  margin-bottom: 0.5rem;
  color: #58a6ff;
}

p {
  color: #8b949e;
  margin-bottom: 1.5rem;
}

input {
  width: 100%;
  padding: 0.65rem 0.85rem;
  background: #0d1117;
  border: 1px solid #30363d;
  border-radius: 6px;
  color: #c9d1d9;
  font-size: 1rem;
  margin-bottom: 1.5rem;
  outline: none;
}

input:focus {
  border-color: #58a6ff;
}

.counter-group {
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: #0d1117;
  border: 1px solid #30363d;
  border-radius: 8px;
  padding: 0.5rem 1rem;
}

button {
  background: #238636;
  color: #ffffff;
  border: none;
  border-radius: 6px;
  width: 36px;
  height: 36px;
  font-size: 1.25rem;
  font-weight: 600;
  cursor: pointer;
  transition: background 0.15s ease;
}

button:hover {
  background: #2ea043;
}

.count-value {
  font-size: 1.5rem;
  font-weight: 600;
  color: #f0f6fc;
}
"""

DEFAULT_MAIN_TS = """import { mount } from 'tokensafe-ui';
import { App } from './App';

const container = document.getElementById('app');
if (container) {
  mount(App, container);
}
"""

DEFAULT_APP_TSX = """import { defineComponent, z, bind, h } from 'tokensafe-ui';

export const App = defineComponent({
  id: 'app-root',
  state: {
    count: z.number().default(0),
    name: z.string().default('Agent'),
  },
  actions: {
    inc: (s) => ({ ...s, count: s.count + 1 }),
    dec: (s) => ({ ...s, count: Math.max(0, s.count - 1) }),
    setName: (s, name: string) => ({ ...s, name }),
  },
  render: ({ state, emit }) =>
    h(
      'div',
      { class: 'container', 'data-agent-id': 'app-root:container' },
      h('h1', { 'data-agent-id': 'app-root:title' }, 'TokenSafe UI'),
      h('p', { 'data-agent-id': 'app-root:greeting' }, `Hello, ${state.name}!`),
      h('input', {
        type: 'text',
        placeholder: 'Enter your name',
        'data-agent-id': 'app-root:input-name',
        ...bind(emit.setName, state.name),
      }),
      h(
        'div',
        { class: 'counter-group', 'data-agent-id': 'app-root:counter-group' },
        h(
          'button',
          { 'data-agent-id': 'app-root:btn-dec', onClick: () => emit.dec() },
          '-'
        ),
        h(
          'span',
          { class: 'count-value', 'data-agent-id': 'app-root:count-value' },
          String(state.count)
        ),
        h(
          'button',
          { 'data-agent-id': 'app-root:btn-inc', onClick: () => emit.inc() },
          '+'
        )
      )
    ),
});
"""

DEFAULT_BACKEND_MAIN_PY = """\"\"\"
{project_name} Backend API instrumented with TokenSafe UARP / OUMV Protocol.
\"\"\"

from typing import List, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from tokensafe_python.adapters.fastapi import mount_tokensafe

app = FastAPI(
    title="{project_name} API",
    description="FastAPI service with native TokenSafe OUMV runtime introspection and simulation.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ItemCreate(BaseModel):
    title: str = Field(..., example="Sample Item")
    price: float = Field(..., gt=0, example=19.99)


class ItemResponse(BaseModel):
    id: int
    title: str
    price: float


# Mock in-memory state
db: List[ItemResponse] = [
    ItemResponse(id=1, title="Initial Product", price=49.99),
]


@app.get("/api/items", response_model=List[ItemResponse], tags=["Items"])
def get_items():
    return db


@app.post("/api/items", response_model=ItemResponse, status_code=201, tags=["Items"])
def create_item(item: ItemCreate):
    new_item = ItemResponse(id=len(db) + 1, title=item.title, price=item.price)
    db.append(new_item)
    return new_item


# Mount TokenSafe UARP Runtime (registers /__oumv/rpc, /__oumv/health, and writes .tokensafe/runtime.json)
mount_tokensafe(app, port=8000)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
"""

DEFAULT_BACKEND_PYPROJECT = """[project]
name = "{project_name}-backend"
version = "0.1.0"
description = "TokenSafe instrumented FastAPI backend service"
readme = "README.md"
requires-python = ">=3.10"
dependencies = [
    "fastapi>=0.110.0",
    "uvicorn>=0.28.0",
    "pydantic>=2.6.0",
    "tokensafe-python>=0.1.0",
]

[build-system]
requires = ["setuptools>=61.0"]
build-backend = "setuptools.build_meta"
"""

DEFAULT_GITIGNORE = """# Dependencies
node_modules/
.venv/
__pycache__/
*.pyc

# Build outputs
dist/
build/
*.egg-info/

# Logs & temp
*.log
.DS_Store
.env
.env.local
"""


class ProjectScaffolder:
    """Creates greenfield TokenSafe ecosystem projects."""

    @classmethod
    def scaffold(
        cls,
        target_dir: str,
        project_name: str,
        template: str = "fullstack",
        force: bool = False
    ) -> Dict[str, Any]:
        """Generates project template files in target_dir."""
        template = template.lower().strip()
        if template not in ("fullstack", "ui", "backend", "python", "fastapi", "go"):
            raise ValueError(f"Unknown template: '{template}'. Valid options: fullstack, ui, backend, python, go")

        if template in ("python", "fastapi"):
            template = "backend"

        os.makedirs(target_dir, exist_ok=True)
        created_files: List[str] = []

        def _write(rel_path: str, content: str):
            full_path = os.path.join(target_dir, rel_path)
            if os.path.exists(full_path) and not force:
                return
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(content)
            created_files.append(rel_path)

        def _write_json(rel_path: str, data: dict):
            full_path = os.path.join(target_dir, rel_path)
            if os.path.exists(full_path) and not force:
                return
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            created_files.append(rel_path)

        # 1. Base files (.tokensafe/runtime.json, .gitignore, README.md, AGENTS.md)
        _write(".gitignore", DEFAULT_GITIGNORE)

        # 2. Template-specific layout
        if template == "fullstack":
            cls._scaffold_ui(target_dir, project_name, subpath="frontend", write_fn=_write, write_json_fn=_write_json)
            cls._scaffold_backend(target_dir, project_name, subpath="backend", write_fn=_write)

            runtime_config = {
                "frontend": {
                    "name": "tokensafe-ui",
                    "version": "0.1.6",
                    "url": "http://localhost:5173/__oumv/rpc"
                },
                "backend": {
                    "name": "tokensafe-python",
                    "version": "0.1.0",
                    "url": "http://localhost:8000/__oumv/rpc"
                }
            }
            _write_json(os.path.join(".tokensafe", "runtime.json"), runtime_config)

            agents_md = cls._generate_agents_md(project_name, is_fullstack=True)
            _write("AGENTS.md", agents_md)
            readme = cls._generate_readme(project_name, template="fullstack")
            _write("README.md", readme)

        elif template == "ui":
            cls._scaffold_ui(target_dir, project_name, subpath="", write_fn=_write, write_json_fn=_write_json)
            runtime_config = {
                "frontend": {
                    "name": "tokensafe-ui",
                    "version": "0.1.6",
                    "url": "http://localhost:5173/__oumv/rpc"
                }
            }
            _write_json(os.path.join(".tokensafe", "runtime.json"), runtime_config)
            agents_md = cls._generate_agents_md(project_name, is_fullstack=False, is_ui=True)
            _write("AGENTS.md", agents_md)
            readme = cls._generate_readme(project_name, template="ui")
            _write("README.md", readme)

        elif template in ("backend", "go"):
            cls._scaffold_backend(target_dir, project_name, subpath="", write_fn=_write)
            runtime_config = {
                "backend": {
                    "name": "tokensafe-python" if template != "go" else "tokensafe-backend",
                    "version": "0.1.0",
                    "url": "http://localhost:8000/__oumv/rpc" if template != "go" else "http://localhost:8080/__oumv/rpc"
                }
            }
            _write_json(os.path.join(".tokensafe", "runtime.json"), runtime_config)
            agents_md = cls._generate_agents_md(project_name, is_fullstack=False, is_ui=False)
            _write("AGENTS.md", agents_md)
            readme = cls._generate_readme(project_name, template="backend")
            _write("README.md", readme)

        return {
            "success": True,
            "project_name": project_name,
            "template": template,
            "target_dir": target_dir,
            "created_files": created_files,
        }

    @classmethod
    def _scaffold_ui(cls, target_dir: str, project_name: str, subpath: str, write_fn, write_json_fn):
        p = lambda rel: os.path.join(subpath, rel) if subpath else rel

        pkg = dict(DEFAULT_FRONTEND_PACKAGE_JSON)
        pkg["name"] = f"{project_name.lower().replace(' ', '-')}-ui"
        write_json_fn(p("package.json"), pkg)
        write_json_fn(p("tsconfig.json"), DEFAULT_TSCONFIG)
        write_fn(p("vite.config.ts"), DEFAULT_VITE_CONFIG)
        write_fn(p("index.html"), DEFAULT_HTML.replace("{project_name}", project_name))
        write_fn(p(os.path.join("src", "style.css")), DEFAULT_STYLE_CSS)
        write_fn(p(os.path.join("src", "main.ts")), DEFAULT_MAIN_TS)
        write_fn(p(os.path.join("src", "App.tsx")), DEFAULT_APP_TSX)

    @classmethod
    def _scaffold_backend(cls, target_dir: str, project_name: str, subpath: str, write_fn):
        p = lambda rel: os.path.join(subpath, rel) if subpath else rel
        clean_name = project_name.replace("-", " ").title()

        main_py = DEFAULT_BACKEND_MAIN_PY.replace("{project_name}", clean_name)
        write_fn(p("main.py"), main_py)

        pyproject = DEFAULT_BACKEND_PYPROJECT.replace("{project_name}", project_name.lower().replace(" ", "-"))
        write_fn(p("pyproject.toml"), pyproject)

    @classmethod
    def _generate_agents_md(cls, project_name: str, is_fullstack: bool = True, is_ui: bool = False) -> str:
        if is_fullstack:
            return f"""# Project Architecture: {project_name}

This project is built using the **TokenSafe Native Ecosystem**:
- **Frontend** (`frontend/`): `tokensafe-ui` (reactive deterministic components, zero-bundle DOM parity) with Vite on port 5173.
- **Backend** (`backend/`): `tokensafe-python` (FastAPI with UARP / OUMV protocol runtime) on port 8000.
- **Auto-Discovery**: Connected via `.tokensafe/runtime.json`.

## AI Coding Agent Directives:
1. **Frontend Changes**: Always write components using `defineComponent` from `tokensafe-ui`. Do not convert to React.
2. **Backend Changes**: Keep routes instrumented with Pydantic and `mount_tokensafe(app)`.
3. **Runtime Verification**: Use `tokensafe:oumv_observe(level="L0")`, `tokensafe:oumv_understand`, and `tokensafe:oumv_verify` to test actions in memory.
"""
        elif is_ui:
            return f"""# Project Architecture: {project_name}

This project is built using **tokensafe-ui**:
- Components are defined with `defineComponent` and Zod state schemas.
- Interactive elements include `data-agent-id` for deterministic agent discovery.
- Use `tokensafe:oumv_observe` and `tokensafe:oumv_verify` for UI inspection.
"""
        else:
            return f"""# Project Architecture: {project_name}

This project is built using **tokensafe-python** (FastAPI):
- Exposes `/__oumv/rpc` and `/__oumv/health` for agent introspection.
- Use `tokensafe:oumv_verify` for transactional testing without touching production databases.
"""

    @classmethod
    def _generate_readme(cls, project_name: str, template: str) -> str:
        return f"""# {project_name}

Greenfield application scaffolded with the **TokenSafe Ecosystem** (`tokensafe create`).

## Tech Stack
- Template: `{template}`
- Frontend: `tokensafe-ui` (Reactive signals, deterministic DOM)
- Backend: `tokensafe-python` (FastAPI + UARP/OUMV protocol)
- AI Agent Tools: `tokensafe` MCP server with 25 tools (AST slicing + OUMV live runtime)

## Quick Start

### 1. Frontend
```bash
cd frontend  # (or root)
npm install
npm run dev
```
Open `http://localhost:5173`

### 2. Backend
```bash
cd backend   # (or root)
pip install -e .
python main.py
```
Backend runs on `http://localhost:8000` with OUMV endpoint at `/__oumv/rpc`.
"""
