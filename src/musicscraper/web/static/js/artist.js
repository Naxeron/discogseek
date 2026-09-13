export function setupArtistDownloader({ startTask, switchTab }) {
  const formArtist = document.getElementById('form-artist-dl');
  if (formArtist) {
    formArtist.addEventListener('submit', async (e) => {
      e.preventDefault();
      const artist = document.getElementById('artist-dl-name').value.trim();
      const format = document.getElementById('artist-dl-format').value;
      const outputDir = document.getElementById('artist-dl-output').value.trim();
      const libraryDir = document.getElementById('artist-dl-lib').value.trim();
      const useSoulseek = document.getElementById('artist-dl-use-slsk').checked;
      const dryRun = document.getElementById('artist-dl-dry-run').checked;

      switchTab('tasks');
      await startTask('artist_download', {
        artist,
        format,
        output_dir: outputDir,
        library_dir: libraryDir,
        use_soulseek: useSoulseek,
        dry_run: dryRun,
      }, `Artist Downloader: ${artist}`);
    });
  }
}

export function openArtistDownloader(artist, switchTab) {
  switchTab('artist');
  document.getElementById('artist-dl-name').value = artist || '';
}
