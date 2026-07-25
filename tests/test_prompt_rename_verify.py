from profile_os.prompt_rename_verify import snapshot, verify


def test_hash_verifier_checks_byte_preservation_and_empty_sections(tmp_path):
    profile = tmp_path / "data" / "profiles" / "example"
    profile.mkdir(parents=True)
    (profile / "base_prompt.md").write_bytes(b"identity\r\n")
    (profile / "role_prompt.md").write_bytes(b"work\x00")
    manifest = snapshot(tmp_path / "data")

    (profile / "base_prompt.md").replace(profile / "who_you_are.md")
    (profile / "role_prompt.md").replace(profile / "what_you_do.md")
    for name in ("signature.md", "lane.md", "voice.md", "how_you_keep_context.md"):
        (profile / name).write_bytes(b"")

    result = verify(tmp_path / "data", manifest)["example"]
    assert result["who_you_are_matches_base_prompt"] is True
    assert result["what_you_do_matches_role_prompt"] is True
    assert all(result["empty_fields"].values())
