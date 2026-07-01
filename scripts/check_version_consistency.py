#!/usr/bin/env python3
import json
import os
import glob
import sys

def main():
    failed = False
    
    # Find all marketplace.json files in the repository
    search_patterns = [
        "**/*/marketplace.json",
        "marketplace.json",
        "*/marketplace.json",
        ".*/**/marketplace.json",
        "**/.*/**/marketplace.json",
        ".*/marketplace.json"
    ]
    
    m_paths = set()
    for pattern in search_patterns:
        m_paths.update(glob.glob(pattern, recursive=True))
        
    for m_path in sorted(list(m_paths)):
        try:
            with open(m_path, 'r') as f:
                data = json.load(f)
        except Exception as e:
            continue

        plugins = data.get("plugins", [])
        for plugin in plugins:
            source = plugin.get("source")
            m_version = plugin.get("version")
            
            # We only care about plugins that declare a version and a source
            if not isinstance(source, str) or not isinstance(m_version, str):
                continue
            
            # The source is usually relative to the repo root, e.g. "./integrations/claude-code"
            # However, glob can find it relative to the current working directory.
            source_dir = os.path.normpath(source)
            
            # Find any plugin.json inside the source directory (it could be nested inside .claude-plugin etc)
            p_paths = set()
            p_patterns = [
                os.path.join(source_dir, "**", "plugin.json"),
                os.path.join(source_dir, ".*", "**", "plugin.json"),
                os.path.join(source_dir, ".*", "plugin.json")
            ]
            for pattern in p_patterns:
                p_paths.update(glob.glob(pattern, recursive=True))
                
            if not p_paths:
                print(f"Warning: Could not find plugin.json for {plugin.get('name')} in {source_dir}")
                continue
                
            for p_path in p_paths:
                try:
                    with open(p_path, 'r') as f:
                        p_data = json.load(f)
                        p_version = p_data.get("version")
                        if not p_version:
                            continue
                            
                        if p_version != m_version:
                            print(f"::error file={p_path}::Version mismatch! {m_path} says {m_version} but {p_path} says {p_version}")
                            failed = True
                        else:
                            print(f"Match: {p_path} ({p_version}) == {m_path} ({m_version})")
                except Exception as e:
                    pass

    if failed:
        print("Version consistency check failed.")
        sys.exit(1)
    else:
        print("All plugin versions match their marketplace.json entries.")

if __name__ == "__main__":
    main()
