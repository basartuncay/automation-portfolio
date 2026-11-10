<?php

namespace App\Console\Commands;

use App\Models\User;
use Illuminate\Console\Command;

class IssueIngestToken extends Command
{
    protected $signature = 'invoices:token {email : the service user that owns the token} {--name=n8n}';

    protected $description = 'Create an API token that may only write invoices (for n8n or the extractor)';

    public function handle(): int
    {
        $user = User::where('email', $this->argument('email'))->first();
        if (! $user) {
            $this->error('No user with that email.');

            return self::FAILURE;
        }
        $this->line($user->createToken($this->option('name'), ['invoices:write'])->plainTextToken);
        $this->comment('Shown once. Store it in n8n credentials as a Bearer token.');

        return self::SUCCESS;
    }
}
