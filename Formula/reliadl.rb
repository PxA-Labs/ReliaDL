# typed: false
# frozen_string_literal: true

# Homebrew Formula for ReliaDL
# Tap repository: PxA-Labs/homebrew-tap
# Installation: brew install pxa-labs/tap/reliadl

class Reliadl < Formula
  desc "Production-grade fault-tolerant parallel file downloader with per-chunk verification"
  homepage "https://github.com/PxA-Labs/ReliaDL"
  version "0.3.0"
  license "Apache-2.0"

  on_macos do
    if Hardware::CPU.arm?
      url "https://github.com/PxA-Labs/ReliaDL/releases/download/v#{version}/reliadl-darwin-arm64"
      # sha256 will be updated by release automation
    else
      url "https://github.com/PxA-Labs/ReliaDL/releases/download/v#{version}/reliadl-darwin-amd64"
      # sha256 will be updated by release automation
    end
  end

  on_linux do
    if Hardware::CPU.arm? && Hardware::CPU.is_64_bit?
      url "https://github.com/PxA-Labs/ReliaDL/releases/download/v#{version}/reliadl-linux-arm64"
      # sha256 will be updated by release automation
    else
      url "https://github.com/PxA-Labs/ReliaDL/releases/download/v#{version}/reliadl-linux-amd64"
      # sha256 will be updated by release automation
    end
  end

  def install
    binary_name = if OS.mac?
      Hardware::CPU.arm? ? "reliadl-darwin-arm64" : "reliadl-darwin-amd64"
    else
      Hardware::CPU.arm? ? "reliadl-linux-arm64" : "reliadl-linux-amd64"
    end

    bin.install binary_name => "reliadl"
  end

  test do
    assert_match "ReliaDL", shell_output("#{bin}/reliadl --help")
    assert_match version.to_s, shell_output("#{bin}/reliadl --version 2>&1", 0)
  end
end
