<?php

namespace Database\Factories;

use Illuminate\Database\Eloquent\Factories\Factory;

class VendorFactory extends Factory
{
    public function definition(): array
    {
        return [
            'name' => fake()->unique()->company(),
            'default_currency' => fake()->randomElement(['USD', 'EUR', 'GBP']),
        ];
    }
}
