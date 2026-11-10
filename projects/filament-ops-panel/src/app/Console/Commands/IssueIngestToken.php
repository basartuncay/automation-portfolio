<?php

namespace App\Console\Commands;

use App\Models\User;
use Illuminate\Console\Command;

class IssueIngestToken extends Command
{
    protected $signature = 'invoices:token {email : the service user that owns the token} {--name=n8n}
        {--token= : use this secret instead of a random one (for docker-compose; at least 32 characters)}';

    protected $description = 'Create an API token that may only write invoices (for n8n, the extractor or the intake worker)';

    public function handle(): int
    {
        $user = User::where('email', $this->argument('email'))->first();
        if (! $user) {
            $this->error('No user with that email.');

            return self::FAILURE;
        }

        $plain = $this->option('token');
        if ($plain === null) {
            $this->line($user->createToken($this->option('name'), ['invoices:write'])->plainTextToken);
            $this->comment('Shown once. Store it in n8n credentials as a Bearer token.');

            return self::SUCCESS;
        }

        if (strlen($plain) < 32) {
            $this->error('--token must be at least 32 characters.');

            return self::FAILURE;
        }
        // Sanctum looks plain (no "id|") tokens up by their sha256, so a known secret can be registered idempotently.
        $user->tokens()->firstOrCreate(['token' => hash('sha256', $plain)], ['name' => $this->option('name'), 'abilities' => ['invoices:write']]);
        $this->info("Token registered for {$user->email}.");

        return self::SUCCESS;
    }
}
